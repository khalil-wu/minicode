// Slash commands and skills advertised by the demo backend. The command list
// is the real catalog captured from a MiniCode backend session.

import type { Json } from "./state";

export const DEMO_COMMANDS: Json[] = [
  {
    "id": "cmd-review",
    "name": "review",
    "command": "review",
    "label": "/review",
    "description": "审查当前项目并按严重级别列出问题",
    "template": "Review the current code changes (staged, unstaged, and untracked files) and provide prioritized findings.",
    "search_text": "review audit code quality bug risk",
    "type": "template",
    "kind": "template",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-export",
    "name": "export",
    "command": "export",
    "label": "/export",
    "description": "导出当前 MiniCode 会话树。",
    "template": "/export",
    "search_text": "export minicode conversation session tree",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-plan",
    "name": "plan",
    "command": "plan",
    "label": "/plan",
    "description": "进入只读 Plan 模式，先探索并提交实施方案",
    "template": "/plan",
    "search_text": "local legacy plan readonly permissions",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-permissions-local",
    "name": "permissions",
    "command": "permissions",
    "label": "/permissions",
    "description": "查看或切换权限模式，并管理规则",
    "template": "/permissions",
    "search_text": "local permissions mode rules list add remove deny override confirm auto plan bypass",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    },
    "args": [
      {
        "value": "confirm",
        "description": "Ask — 写操作需确认"
      },
      {
        "value": "diff",
        "description": "Diff review — 改动需审查"
      },
      {
        "value": "auto",
        "description": "Auto — 自动执行常规操作"
      },
      {
        "value": "rules list",
        "description": "查看权限规则"
      }
    ]
  },
  {
    "id": "cmd-effort",
    "name": "effort",
    "command": "effort",
    "label": "/effort",
    "description": "Set a reasoning effort advertised by the active model.",
    "template": "/effort high",
    "search_text": "local effort reasoning none minimal low medium high xhigh max ultra",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    },
    "args": [
      {
        "value": "none",
        "description": "关闭 reasoning（供应商支持时）"
      },
      {
        "value": "minimal",
        "description": "最少推理"
      },
      {
        "value": "low",
        "description": "最快，适合简单任务"
      },
      {
        "value": "medium",
        "description": "平衡速度与质量"
      },
      {
        "value": "high",
        "description": "更深入的推理"
      },
      {
        "value": "xhigh",
        "description": "OpenAI 官方最高推理档"
      },
      {
        "value": "max",
        "description": "最强推理，最慢"
      },
      {
        "value": "ultra",
        "description": "模型声明支持时使用 Ultra"
      }
    ]
  },
  {
    "id": "cmd-skill-local",
    "name": "skill",
    "command": "skill",
    "label": "/skill",
    "description": "选择一个技能用于下一条消息",
    "template": "/skill",
    "search_text": "local skill select invoke",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-skills-local",
    "name": "skills",
    "command": "skills",
    "label": "/skills",
    "description": "管理和安装技能",
    "template": "/skills",
    "search_text": "local skills browse select install",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-model-local",
    "name": "model",
    "command": "model",
    "label": "/model",
    "description": "选择或配置当前模型",
    "template": "/model",
    "search_text": "local model provider reasoning",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-mcp-local",
    "name": "mcp",
    "command": "mcp",
    "label": "/mcp",
    "description": "管理 MCP 连接器和工具",
    "template": "/mcp",
    "search_text": "local mcp connectors tools",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-plugins-local",
    "name": "plugins",
    "command": "plugins",
    "label": "/plugins",
    "description": "管理本地 MiniCode 插件",
    "template": "/plugins",
    "search_text": "local plugins bundles skills mcp apps hooks",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-new-local",
    "name": "new",
    "command": "new",
    "label": "/new",
    "description": "新建空白会话",
    "template": "/new",
    "search_text": "local new conversation",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-clear-local",
    "name": "clear",
    "command": "clear",
    "label": "/clear",
    "description": "清空当前会话历史",
    "template": "/clear",
    "search_text": "local clear composer attachments",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-compact-local",
    "name": "compact",
    "command": "compact",
    "label": "/compact",
    "description": "手动压缩对话上下文以释放 token 预算",
    "template": "/compact",
    "search_text": "local compact context summary tokens",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-goal-local",
    "name": "goal",
    "command": "goal",
    "label": "/goal",
    "description": "Set or manage a persistent conversation goal.",
    "template": "/goal",
    "search_text": "local goal objective target pause resume clear",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    },
    "args": [
      {
        "value": "set",
        "description": "设定目标（后跟目标描述）"
      },
      {
        "value": "pause",
        "description": "暂停当前目标"
      },
      {
        "value": "resume",
        "description": "恢复目标"
      },
      {
        "value": "clear",
        "description": "清除目标"
      }
    ]
  },
  {
    "id": "cmd-memory-local",
    "name": "memory",
    "command": "memory",
    "label": "/memory",
    "description": "切换会话记忆模式",
    "template": "/memory enabled",
    "search_text": "local memory mode enabled disabled",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    },
    "args": [
      {
        "value": "enabled",
        "description": "允许生成长期记忆"
      },
      {
        "value": "disabled",
        "description": "禁止生成长期记忆"
      }
    ]
  },
  {
    "id": "cmd-archive-local",
    "name": "archive",
    "command": "archive",
    "label": "/archive",
    "description": "归档当前会话",
    "template": "/archive",
    "search_text": "local archive conversation",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-unarchive-local",
    "name": "unarchive",
    "command": "unarchive",
    "label": "/unarchive",
    "description": "取消归档当前会话",
    "template": "/unarchive",
    "search_text": "local unarchive restore conversation",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-tasks-local",
    "name": "tasks",
    "command": "tasks",
    "label": "/tasks",
    "description": "查看当前会话任务状态",
    "template": "/tasks",
    "search_text": "local runtime tasks status running pending failed",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-status-local",
    "name": "status",
    "command": "status",
    "label": "/status",
    "description": "查看当前运行指标",
    "template": "/status",
    "search_text": "local runtime status metrics counters",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-usage-local",
    "name": "usage",
    "command": "usage",
    "label": "/usage",
    "description": "查看当前会话 token、上下文和成本用量",
    "template": "/usage",
    "search_text": "local usage cost token context budget",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-context-local",
    "name": "context",
    "command": "context",
    "label": "/context",
    "description": "查看上下文 token 用量和压缩阈值",
    "template": "/context",
    "search_text": "local context token budget compact",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-cost-local",
    "name": "cost",
    "command": "cost",
    "label": "/cost",
    "description": "查看当前上下文及本次应用运行期的成本和模型用量",
    "template": "/cost",
    "search_text": "local cost usage spend price",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-init-template",
    "name": "init",
    "command": "init",
    "label": "/init",
    "description": "分析代码库并生成 .minicode/INSTRUCTIONS.md 项目说明",
    "template": "Generate a file named .minicode/INSTRUCTIONS.md that serves as a contributor guide for this repository.\nBefore writing, check whether .minicode/INSTRUCTIONS.md already exists. If it does, do not overwrite or modify it.\nIf the repository has an existing AGENTS.md, CLAUDE.md, or similar agent instruction file, read it first and carry the still-accurate parts across; leave the original file in place.\nCreate a concise repository-specific guide with actionable sections for project structure, build/test/development commands, coding conventions, testing, and commit or pull-request guidance. Omit sections that do not apply and do not invent project facts.",
    "search_text": "template init instructions agentsmd documentation bootstrap",
    "type": "template",
    "kind": "template",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-help-local",
    "name": "help",
    "command": "help",
    "label": "/help",
    "description": "查看本地命令帮助",
    "template": "/help",
    "search_text": "local help commands",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  },
  {
    "id": "cmd-resume-local",
    "name": "resume",
    "command": "resume",
    "label": "/resume",
    "description": "从上次中断的任务恢复继续（断点续跑）",
    "template": "/resume",
    "search_text": "local resume checkpoint recover continue retry",
    "type": "local",
    "kind": "local",
    "source": "builtin",
    "enabled": true,
    "availability": {
      "kind": "always",
      "scope": "session"
    }
  }
];

export const DEMO_SKILLS: Json[] = [
  {
    "name": "frontend-design",
    "description": "按设计系统构建精致的前端界面，关注层级、间距与动效。",
    "display_name": "Frontend Design",
    "short_description": "打磨界面细节、动效与排版",
    "icon": "",
    "icon_large": "",
    "brand_color": "#d97757",
    "path": "/Users/demo/.minicode/skills/frontend-design/SKILL.md",
    "active": false,
    "level": "builtin",
    "source_level": "builtin",
    "mcp_dependencies": [],
    "allow_implicit_invocation": true,
    "user_invocable": true,
    "default_prompt": ""
  },
  {
    "name": "browser",
    "description": "在用户授权的本地浏览器中完成导航、检查页面状态和调试真实交互流程。",
    "display_name": "Browser",
    "short_description": "在授权的浏览器中检查页面与交互",
    "icon": "",
    "icon_large": "",
    "brand_color": "#3b82f6",
    "path": "/Users/demo/.minicode/skills/browser/SKILL.md",
    "active": false,
    "level": "builtin",
    "source_level": "builtin",
    "mcp_dependencies": [],
    "allow_implicit_invocation": true,
    "user_invocable": true,
    "default_prompt": ""
  },
  {
    "name": "pdf",
    "description": "提取 PDF 文本与表格，合并拆分页面，填写表单字段。",
    "display_name": "PDF",
    "short_description": "读取、拆分与填写 PDF",
    "icon": "",
    "icon_large": "",
    "brand_color": "#e5484d",
    "path": "/Users/demo/.minicode/skills/pdf/SKILL.md",
    "active": false,
    "level": "builtin",
    "source_level": "builtin",
    "mcp_dependencies": [],
    "allow_implicit_invocation": true,
    "user_invocable": true,
    "default_prompt": ""
  },
  {
    "name": "release-notes",
    "description": "汇总提交与合并请求，生成结构清晰的发布说明。",
    "display_name": "Release Notes",
    "short_description": "根据提交生成发布说明",
    "icon": "",
    "icon_large": "",
    "brand_color": "#30a46c",
    "path": "/Users/demo/.minicode/skills/release-notes/SKILL.md",
    "active": false,
    "level": "builtin",
    "source_level": "builtin",
    "mcp_dependencies": [],
    "allow_implicit_invocation": true,
    "user_invocable": true,
    "default_prompt": ""
  }
];
