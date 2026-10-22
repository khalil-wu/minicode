import type { Tab } from "./settingsShared";
import type { TranslationKey } from "../i18n";

type SettingsOption = { tab: Tab; title: TranslationKey; description: TranslationKey; keywords: string };
const options = (tab: Tab, entries: [TranslationKey, TranslationKey, string][]): SettingsOption[] =>
  entries.map(([title, description, keywords]) => ({ tab, title, description, keywords }));

export const SETTINGS_OPTIONS: SettingsOption[] = [
  ...options("general", [
    ["语言", "应用界面的语言。", "language locale English 简体中文 自动检测 auto detect"],
    ["发送快捷键", "选择 Enter 或 Ctrl+Enter 发送消息。", "发送 换行 输入"],
    ["纯文本输入", "编写消息时，将代码、Markdown 和链接保留为字面文本。", "plain text rich composer 富文本 输入 代码 列表 链接"],
    ["默认终端位置", "选择终端快捷键和环境动作打开终端标签页的位置。", "terminal bottom right 底部 右侧 环境"],
    ["跟进行为", "选择运行中的消息排队或引导方式。", "队列 补充 消息"],
    ["更新通道", "检查桌面应用更新。", "版本 桌面 更新"],
  ]),
  ...options("appearance", [
    ["编辑器", "配置缩进、保存时格式化、标签及代码模板。", "大纲 符号 缩略图 自动换行 模板 代码补全"],
    ["AI 代码预测", "配置行内预测的模型和输出上限。", "inline 灰字 补全 tokens 模型"],
    ["主题", "使用系统、浅色或深色主题。", "暗色 外观"],
    ["字体", "选择应用界面的字体。", "界面 字体 font family 特殊 字体"],
    ["界面字体样式", "选择当前界面字体的实际字形。", "font style weight 字重 粗体 斜体"],
    ["内容字体", "选择对话正文的字体与实际字形。", "content font family style 阅读 Markdown"],
    ["代码字体", "选择等宽代码字体与实际字形。", "code font family style monospace 等宽 编辑器 终端"],
    ["界面字号", "调整应用界面的基础字号", "字号 字体 px"],
    ["代码字号", "调整聊天和差异中代码的基础字号", "字号 代码 字体 px diff"],
    ["减少动态效果", "减少动画，或跟随系统设置", "减少动画 动效 无动画 system on off"],
    ["分别设置浅色和深色模式", "为每种模式选择主题、颜色和字体", "light dark separate 浅色 深色"],
    ["对比度", "调整浅色或深色主题的对比度。", "contrast 主题 背景 前景"],
    ["差异标记", "使用颜色或 +/- 标记显示更改", "diff markers symbols 颜色 符号 更改"],
    ["使用手形指针", "悬停在可交互元素上时显示手形指针", "pointer cursor mouse 鼠标 光标 手形"],
  ]),
  ...options("voice", [
    ["麦克风", "选择用于听写的输入设备。", "语音 设备"],
    ["最近录音", "下载、重试转录或删除最近 20 段本机录音。", "语音 转录 history recordings"],
    ["听写词典", "编辑听写时应识别的词语或短语。", "语音 dictionary 词汇"],
    ["听写快捷键", "按住即可听写，双击可免提听写。", "dictation hold double tap global 单击 免提"],
  ]),
  ...options("computerUse", [
    ["始终允许的应用", "管理 MiniCode 如何使用电脑上的其他应用。", "computer use apps approvals always allow 电脑 应用 授权 始终允许"],
  ]),
  ...options("personalization", [
    ["自定义指令", "编辑所有任务共用的 INSTRUCTIONS.md。", "用户 偏好 prompt 规则"],
    ["长期记忆生成", "控制当前任务是否生成长期记忆。", "记忆 摘要"],
    ["清除长期记忆", "清除生成的记忆和派生摘要。", "记忆 重置"],
    ["当前指令来源", "查看项目、用户和规则的加载来源。", "AGENTS.md INSTRUCTIONS.md"],
  ]),
  ...options("provider", [
    ["听写模型", "配置服务商提供的音频转录模型。", "语音 转录 transcription dictation provider"],
    ["显示名称", "设置提供商在模型菜单中的名称。", "名称 提供商"],
    ["提供商类型", "选择接口提供商。", "OpenAI Anthropic 自定义"],
    ["接口地址", "配置模型接口的 Base URL。", "api url 服务"],
    ["API 密钥", "配置模型请求的认证凭据。", "api key token 密钥 认证"],
    ["API 格式", "选择 Responses、Chat 或 Anthropic 协议。", "协议 接口"],
    ["模型", "管理模型映射、默认模型和上下文窗口。", "模型 id 上下文"],
    ["OAuth 登录", "管理提供商的 OAuth 认证。", "登录 授权"],
    ["网络连接", "配置代理连接方式。", "代理 网络 proxy"],
    ["当前模型行为", "配置模型的推理和工具行为。", "工具 推理 instructions"],
    ["Responses 提示词缓存", "设置 Responses API 提示词缓存。", "缓存 cache retention"],
    ["Responses 推理摘要", "设置模型返回的可见推理摘要。", "reasoning 思考"],
    ["扩展思考 Token 预算", "设置 Anthropic 扩展思考预算。", "思考 预算 thinking"],
    ["请求最大输出 Token", "设置单次输出上限。", "token 输出 长度"],
    ["辅助模型", "选择轻量辅助任务的模型。", "small fast model"],
  ]),
  ...options("browser", [
    ["浏览器", "让 MiniCode 控制内置浏览器", "网页 预览 控制"],
    ["浏览数据", "清除内置浏览器的历史记录、网站数据、缓存和下载记录。", "下载 文件 网站 cookie 清理"],
    ["浏览历史", "查看和管理在内置浏览器中访问过的页面。", "history 历史 网站"],
  ]),
  ...options("workspaceGit", [
    ["分支前缀", "MiniCode 创建新分支时使用的前缀", "git branch prefix"],
    ["Pull Request 合并方式", "选择 MiniCode 合并 Pull Request 的方式", "git merge squash"],
    ["始终强制推送", "从 MiniCode 推送时使用 --force-with-lease", "git push force"],
    ["创建草稿 Pull Request", "从 MiniCode 创建 PR 时默认使用草稿", "git draft pr"],
    ["提交指令", "添加到提交说明生成提示词中", "git commit instructions"],
    ["Pull Request 指令", "添加到 PR 标题和说明生成提示词中", "git pr instructions"],
    ["审查结果呈现方式", "尽可能在当前聊天中启动 /review，或启动单独的审查聊天", "review inline detached 审阅 独立"],
  ]),
  ...options("connectors", [
    ["MCP 服务", "管理服务连接、登录和能力目录。", "mcp 工具 服务 资源"],
    ["服务名称", "创建或编辑 MCP 服务配置。", "添加 命令 参数 地址"],
  ]),
  ...options("skills", [["技能", "查看技能来源、可调用性和当前选择。", "SKILL.md 工作流 技能 安装 目录"]]),
  ...options("shortcuts", [
    ["数字快捷键", "选择数字快捷键优先切换聊天或标签页。", "number shortcuts Ctrl Alt Command tabs chats 数字 标签 聊天"],
    ["在默认浏览器中打开网页链接", "按住指定按键并点击网页链接，即可在系统默认浏览器中打开。", "external browser link modifier click Ctrl Alt Shift 修饰键 单击"],
  ]),
  ...options("plugins", [
    ["已安装插件", "查看能力包并管理启用状态。", "插件 安装 卸载"],
    ["插件来源", "添加或移除插件市场来源。", "github git 来源"],
  ]),
  ...options("advanced", [
    ["本地环境", "本地环境用于配置项目工作树的设置脚本和操作。", "environment setup cleanup actions 平台 工作树 设置脚本 清理脚本 操作"],
  ]),
  ...options("pets", [["选择宠物", "Mini 管理聊天并显示需要关注的事项", "mini pets avatar sprite hatch 宠物 动画 自定义 大小"]]),
  ...options("import", [
    ["从其他 AI 应用导入", "将其他 AI 应用的配置、项目和聊天导入 MiniCode", "Claude Code Cursor 聊天 指令 设置 技能 插件 MCP agents hooks commands 记忆"],
    ["自动同步", "自动同步已连接来源中新增和更新的内容", "同步 导入 sync"],
  ]),
  ...options("config", [
    ["审批策略", "选择 MiniCode 何时请求批准。", "approval policy 权限"],
    ["允许网络访问", "允许工作区写入沙箱访问网络。", "network sandbox 网络"],
    ["回复详细程度", "选择 MiniCode 在回复中包含多少细节。", "verbosity low medium high"],
    ["推理摘要", "选择 MiniCode 如何概述推理过程。", "reasoning summary"],
  ]),
  ...options("hooks", [["钩子", "管理来自配置和已启用插件的生命周期钩子。", "hooks lifecycle trusted command"]]),
  ...options("storage", [["清理旧聊天", "永久删除前，先查看聊天及其子智能体。", "storage cleanup subagent 清理 删除 存储"]]),
  ...options("worktrees", [
    ["工作树根目录", "MiniCode 创建托管工作树的目录。留空则使用默认位置。", "worktree root 文件夹"],
    ["自动删除旧工作树", "建议保持开启，以免旧工作树占用不必要的磁盘空间。", "worktree cleanup prune"],
  ]),
  ...options("scheduler", [["任务名称", "创建或编辑任务时间、提示词和工作区。", "定时 计划 cron 时区 自动"]]),
  ...options("archived", [["搜索已归档任务", "按任务标题、项目和日期找回对话。", "归档 历史 恢复 搜索"]]),
];
