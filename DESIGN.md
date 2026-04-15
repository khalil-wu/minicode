# MiniCode Agent 完整设计文档

> 核心哲学：上下文是稀缺的注意力资源，不是垃圾桶。
> 目标是找到**最小的一组高信息密度 token**，最大化模型产生目标结果的概率。

---

## 零、设计基础：Context Engineering 的六个组件

本项目所有设计决策均从这个公式出发：

```
C = A(cinstr, cknow, ctools, cmem, cstate, cquery)
```

| 组件 | 含义 | 在 MiniCode 中对应 |
|------|------|------------------|
| `cinstr` | 角色定义、行为规范、输出格式 | System Prompt + 激活的 Skill 指令 |
| `cknow` | 外部知识，RAG 检索块 | docparse / code-index MCP 返回内容 |
| `ctools` | 可用工具的 schema | ToolRegistry.get_schemas() |
| `cmem` | 跨会话持久记忆 | MEMORY.md 索引 + 向量记忆 recall |
| `cstate` | 当前任务状态、已完成步骤 | AgentState（任务树、artifact 引用） |
| `cquery` | 用户当下请求 | 用户消息 |

**黄金原则**：任何一个组件注入 context 都必须问自己——"如果去掉它，模型回答会变差吗？"。如果不会，不要注入。

---

## 一、整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                      Frontend (React)                        │
│    ChatPanel / ToolCallView / DiffReview / SkillSelector     │
└──────────────────────────┬──────────────────────────────────┘
                           │ WebSocket（消息层，轻量）
┌──────────────────────────▼──────────────────────────────────┐
│                   Backend (FastAPI)                          │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │           Context Engineering Pipeline               │   │
│  │  渐进式披露：默认只给索引，按需展开细节                  │   │
│  │  [cinstr] [cmem索引] [ctools摘要] [cstate] [cquery]  │   │
│  └────────────────────────┬─────────────────────────────┘   │
│                           │                                  │
│  ┌────────────────────────▼─────────────────────────────┐   │
│  │              Agent Loop（四级进化）                   │   │
│  │  L1:对话 → L2:工具循环 → L3:审批 → L4:完整            │   │
│  │  终止条件：完成 / 预算耗尽 / 停滞检测 / 异常兜底         │   │
│  └────────┬────────────────────────────┬────────────────┘   │
│           │                            │                     │
│  ┌────────▼────────┐        ┌──────────▼──────────┐         │
│  │   LLM Adapter   │        │    Tool Registry     │         │
│  │  OpenAI/Claude  │        │  builtin + MCP动态   │         │
│  └─────────────────┘        └──────────┬───────────┘         │
│                                        │                     │
│  ┌─────────────────────────────────────▼───────────────┐    │
│  │                  MCP Client Layer                    │    │
│  │  stdio: websearch / docparse / memory-rag / code-idx │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                              │
│  ┌───────────────────┐    ┌─────────────────────────┐       │
│  │   Memory System   │    │    Artifact Store        │       │
│  │  文件索引+向量库   │    │  工具大输出的外部存储     │       │
│  └───────────────────┘    └─────────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

---

## 二、记忆系统设计

### 2.1 短期记忆（In-Context Memory）

当前 context window 内的信息，随会话结束消失。

```
短期记忆内容（按优先级排列）：
├── cinstr: system_prompt + 激活 skill 的指令内容
├── cmem:   MEMORY.md 索引摘要（不是全文，只是一行描述）
├── ctools: 工具 schema（按 token 预算可裁剪描述）
├── cstate: AgentState（任务进度、artifact 引用列表）
├── cknow:  RAG 检索块（JIT 注入，非预装）
├── history: 对话历史（按预算裁剪）
└── cquery: 当前用户消息
```

**Token Budget 分配（128K 窗口）**：

```
cinstr  (System Prompt):    ~2K   固定，高优先级
cinstr  (Active Skills):    ~4K   按需，触发才注入
cmem    (Memory Index):     ~1K   只注入 MEMORY.md 索引行，不注入正文
ctools  (Tool Schemas):     ~6K   只注入 schema，不注入示例
cstate  (Agent State):      ~2K   任务树摘要 + artifact 引用
cknow   (RAG chunks):       ~8K   JIT 注入，非预装
history (Conversation):     ~87K  滚动，compaction 后释放
Response Reserve:           ~8K
```

**关键原则**：`cmem` 启动时只注入 MEMORY.md 的索引行（每行 ~50 tokens），Agent 需要具体记忆时通过 `read_memory` 工具按需读取——这是 Progressive Disclosure 的直接体现。

**Compaction 触发与策略**（触发阈值：当前使用量 > 0.75 * TOTAL）：

```
Step 1: 压缩 tool_result       ── 将长工具结果替换为摘要 + artifact 引用
Step 2: 摘要早期对话            ── 用 LLM 将 20 轮前的对话压缩为一段摘要
Step 3: 滑动窗口兜底            ── 只保留最近 15 轮（最后手段）

保留原则：最近 15 轮 + 含工具调用的关键轮次 + 用户明确的约束指令
```

### 2.2 长期记忆（Persistent Memory）

跨会话持久化，分为两种互补存储：

#### A. 文件记忆（结构化，透明可审计）

```
data/memory/
├── MEMORY.md              # 索引文件（启动时加载，每条 ≤ 80 chars）
├── user_profile.md        # 用户偏好、技术栈、工作风格
├── project_context.md     # 项目背景、当前目标、已决策事项
├── feedback.md            # 用户对 Agent 行为的纠正记录
└── reference.md           # 外部资源、文档链接
```

MEMORY.md 格式（启动注入的是这个索引，不是正文）：
```markdown
- [user_profile](user_profile.md) — 全栈工程师，TypeScript 优先，不喜欢冗余注释
- [project_context](project_context.md) — MiniCode 学习项目，当前 Phase 2，工具循环开发中
- [feedback](feedback.md) — 不要自动 commit；写文件前先展示计划
```

Agent 工具：`read_memory(file)` 读取具体文件，`save_memory(file, content)` 写入。

#### B. 向量记忆（语义检索）

```
向量记忆用途：
├── 历史对话的重要片段（按重要性筛选后入库）
├── 解决过的 bug + 方案
├── 代码片段 + 背景说明
└── 项目文档的 chunk（与 docparse 共用 ChromaDB）
```

**读写时机**：
- 读：Context 构建时被动检索（Top-K 静默注入）+ Agent 主动调用 `recall` 工具（Agentic RAG）
- 写：会话结束后 Agent 自动总结本轮重要内容调用 `remember` 工具入库

### 2.3 记忆层级与加载策略

```
╔══════════════════════════════════════════════╗
║          Context Window（短期记忆）           ║  ← 当前对话
║  [cinstr][cmem索引][ctools][cstate][history] ║
╚══════════════════════════╦═══════════════════╝
                           ║ Agent 按需读取（工具调用）
╔══════════════════════════╩═══════════════════╗
║         持久化存储（长期记忆）                 ║
║  ┌──────────────────┐  ┌──────────────────┐  ║
║  │  文件记忆         │  │   向量记忆        │  ║
║  │  MEMORY.md 索引  │  │   ChromaDB       │  ║  ← 长期记忆
║  │  按需读取全文     │  │   语义检索        │  ║
║  └──────────────────┘  └──────────────────┘  ║
╚═══════════════════════════════════════════════╝
                           ║ 工具大输出落地
╔══════════════════════════╩═══════════════════╗
║         Artifact Store（产物层）              ║
║  大文件内容 / 工具原始输出 / 搜索结果全文      ║  ← 按引用访问
╚═══════════════════════════════════════════════╝
```

---

## 三、Context 构建流水线（渐进式披露）

**核心原则**：不要预先决定所有信息都该进入；让系统在运行中逐层揭示。

### 3.1 启动时的最小化 Context（每次对话开始）

```
启动 Context（只加载这些，不多加）：

cinstr:  System Prompt 基础版（角色 + 核心规则，~1K）
cmem:    MEMORY.md 索引（只有每条的一行摘要，~500 tokens）
ctools:  工具名称列表 + 一行描述（不含参数细节，~1K）
cstate:  空 AgentState（新会话）
cquery:  用户消息
```

### 3.2 动态注入触发条件

| 触发条件 | 注入内容 | 注入时机 |
|---------|---------|---------|
| 用户消息匹配 Skill 触发词 | 对应 SKILL.md 完整内容 | 调用 LLM 前 |
| Agent 调用 `recall(query)` | 向量检索结果 Top-K | 工具执行后追加到 cknow |
| Agent 调用 `read_memory(file)` | 具体记忆文件内容 | 工具执行后追加到 cmem |
| Agent 调用 `search_code(query)` | 代码片段检索结果 | 工具执行后追加到 cknow |
| 用户上传文档 | docparse 解析结果索引 | 不直接注入，存 artifact，给 Agent 引用 |

### 3.3 完整 Context 组装顺序（实际调用 LLM 前）

```python
def build_context(user_message: str, state: AgentState) -> list[Message]:
    """
    渐进式 Context 组装：先给索引，按需已有内容再追加
    """
    messages = []

    # 1. cinstr: 基础系统提示（固定，每次都有）
    system = BASE_SYSTEM_PROMPT

    # 2. cinstr: 激活的 Skill 指令（只有已激活的才加）
    if state.active_skills:
        system += skill_manager.get_instructions(state.active_skills)

    # 3. cmem: 只注入 MEMORY.md 索引（不是正文！）
    system += f"\n\n## 可用记忆（需要时用 read_memory 工具读取详情）\n{memory.get_index()}"

    # 4. cstate: 当前任务状态（如有）
    if state.task_summary:
        system += f"\n\n## 当前任务状态\n{state.task_summary}"

    # 5. cknow: 本轮已检索到的知识块（JIT，不预装）
    if state.retrieved_chunks:
        system += format_rag_chunks(state.retrieved_chunks)

    messages.append({"role": "system", "content": system})

    # 6. history: 对话历史（按预算裁剪）
    messages.extend(context.get_history_within_budget())

    # 7. cquery: 用户消息
    messages.append({"role": "user", "content": user_message})

    return messages
```

### 3.4 工具输出的 Token 控制（Artifact 模式）

工具返回大内容时，不直接塞进 context，而是写入 Artifact Store，只在 context 中保留引用：

```python
class ToolResult:
    content: str          # 短摘要（≤ 500 tokens，始终注入 context）
    artifact_id: str | None  # 如果内容很长，存 artifact，给 Agent 引用
    artifact_preview: str | None  # 前 3 行预览，注入 context

# 工具执行后注入 context 的格式：
"""
工具 search_code 执行完成：
找到 8 个相关片段，最相关的是 src/agent/loop.py:42-58（loop 主逻辑）
完整结果已存储，可用 read_artifact('artifact_001') 获取详情
"""
```

---

## 四、RAG 集成（双模式）

RAG 不是一个独立模块，而是两种使用方式的组合：

### 4.1 被动 RAG（Context 构建时静默注入）

适合：每次对话都高度相关的背景知识（如项目文档概要）

```
触发：会话开始时，用 user_message 做向量检索
策略：Top-K=3，相关性阈值 0.75，注入为 <background> 块
位置：注入到 cknow，在 system prompt 末尾
限制：总量控制在 3K tokens 内
```

### 4.2 主动 RAG（Agent 工具调用驱动）

适合：Agent 在推理过程中发现需要某类知识时主动检索

```
Agent 调用：
  recall(query="如何处理 PDF 解析错误")
  search_code(query="文件写入权限检查逻辑")
  mcp__docparse__parse_url(url="...")

结果处理：
  1. 短摘要注入 context（≤ 500 tokens）
  2. 完整内容写入 Artifact Store
  3. Agent 可以 read_artifact 按需获取全文
```

### 4.3 向量存储

```
ChromaDB（推荐，本地文件，零配置）
  collections:
    - "memory"      → 长期记忆片段
    - "documents"   → docparse 解析的文档 chunk
    - "codebase"    → code-index 索引的代码片段

Embedding 模型：
  - 首选：text-embedding-3-small（OpenAI，低成本）
  - 本地：bge-m3（中文支持好，无 API 费用）

分块策略：
  - 通用文档：512 tokens，overlap=64
  - 代码文件：按函数/类边界分割（tree-sitter），保留完整语义单元
  - 对话历史：按"话题转换"分割，而非固定长度
```

### 4.4 何时用 RAG vs 其他方式

| 信息类型 | 使用方式 |
|---------|---------|
| 实时网页信息 | websearch MCP（不用 RAG） |
| 已上传的文档 | docparse 解析 → 向量入库 → 主动 RAG |
| 项目代码库 | code-index 索引 → 主动 RAG |
| 历史决策/偏好 | 文件记忆（read_memory）+ 被动 RAG |
| 当前对话上下文 | 直接在 history 中（不用 RAG） |

---

## 五、Progressive Disclosure：Skills 三层设计

Skills 是渐进式披露最清晰的体现——**Skill 本身就是按层组织的知识包**。

### 5.1 三层加载模型

```
Layer 1 (始终加载，~20 tokens/skill)：
  name: frontend-dev
  description: React 18 + TypeScript + Tailwind 专家模式

Layer 2 (触发时加载完整 SKILL.md，~1-4K tokens)：
  ## 角色 / ## 编码规范 / ## 工作流程 / ## 输出格式

Layer 3 (Agent 按需读取，不预装)：
  linked_resources:
    - examples/react-component-template.tsx
    - refs/tailwind-patterns.md
    - scripts/type-check.sh
```

启动时 context 只有 Layer 1（所有 skill 的名称+描述列表）。Agent 或触发词匹配才加载 Layer 2。Agent 在执行任务时才按需读取 Layer 3 资源。

### 5.2 SKILL.md 完整格式规范

```markdown
---
name: frontend-dev
description: React 18 + TypeScript + Tailwind 专家模式（一行，用于 Layer 1）
version: 1.0.0
triggers:           # 自动激活的关键词（用于 Layer 1 匹配）
  - "写组件"
  - "React"
  - "前端"
conflicts:          # 与哪些 skill 冲突（同时激活时优先保留最新）
  - backend-dev
tools_required:
  - read_file
  - write_file
mcp_required:
  - websearch
linked_resources:   # Layer 3：Agent 按需读取，不预装
  - examples/react-component-template.tsx
  - refs/tailwind-patterns.md
---

## 角色

你是一名资深前端工程师，专注于 React 18 + TypeScript + Tailwind CSS。

## 编码规范

- 使用函数式组件 + hooks，禁用 class 组件
- 所有 props 必须定义 TypeScript 接口
- 样式只用 Tailwind，不写内联 style

## 工作流程

1. 先 read_file 读取现有同类组件，了解设计模式
2. 新建组件时参考目录下已有风格
3. 完成后运行 `npm run type-check`

## 输出格式

修改文件后列出：变更文件 | 变更原因 | 下一步
```

### 5.3 Skills 加载逻辑

```python
class SkillManager:
    # ── 发现 ──
    def discover(self) -> list[SkillMeta]:
        """扫描三级目录，只加载 Layer 1（name+description），不读 SKILL.md 正文"""
        # 优先级：项目级 > 全局级 > 内置
        ...

    # ── 激活 ──
    def auto_detect(self, user_message: str) -> list[str]:
        """关键词匹配，返回应激活的 skill name 列表"""
        ...

    def load(self, skill_name: str) -> str:
        """加载 Layer 2（SKILL.md 正文），注入 system prompt"""
        ...

    def get_layer1_summary(self) -> str:
        """返回所有 skill 的 Layer 1 摘要，始终注入 context（很短）"""
        return "\n".join(f"- {s.name}: {s.description}" for s in self.discovered)
```

---

## 六、MCP 服务设计

MCP 的核心价值：**工具解耦**——能力独立开发、独立部署、被任意 Agent 复用。

### 6.1 协议核心概念

```
MCP Server 三种能力：
├── Tools      ── 有副作用的函数调用（Agent 主动触发）
├── Resources  ── 无副作用的数据读取（URI 寻址，支持 subscribe 订阅变化）
└── Prompts    ── 预定义提示词模板（Agent 主动调用，注入 context）

传输层：
├── stdio      ── 本地子进程，管道通信（适合包装 CLI 工具）
└── HTTP SSE   ── 远程服务，Streamable HTTP（适合云服务）
```

### 6.2 内置 MCP Server 详细设计

所有 Server 均遵循**Token-efficient 输出原则**：默认返回摘要+引用，Agent 需要全文时用 `get_full_result`。

---

#### `websearch` —— 网页搜索

```python
# 传输：stdio  端口：无（子进程）

Tools:
  search(query: str, num_results: int = 5) -> SearchSummary
    # 返回：结果列表（title + url + 1行摘要），总量 ≤ 300 tokens
    # 完整片段存 artifact，按需读取

  fetch_page(url: str, extract: "text"|"markdown" = "markdown") -> PageSummary
    # 返回：页面标题 + 前 500 tokens 正文摘要 + artifact_id（完整内容）

Resources:
  search://recent   ── 最近 10 次搜索结果索引

实现依赖：duckduckgo-search（无需 key）/ Serper API（可选，效果更好）
          trafilatura（正文提取）
```

---

#### `docparse` —— 文档解析

```python
# 传输：stdio

Tools:
  parse(source: str) -> ParseSummary
    # source 可以是文件路径或 URL
    # 返回：文档标题 + 结构概览 + 页数/字数 + doc_id
    # 完整内容写入 Artifact Store

  chunk_and_index(doc_id: str, chunk_size: int = 512) -> IndexStats
    # 分块 + 向量入库 ChromaDB
    # 返回：chunk 数量、入库成功数

  get_chunk(doc_id: str, chunk_index: int) -> str
    # 按需读取具体 chunk

Resources:
  doc://{doc_id}          ── 文档元数据（标题、来源、大小、chunk 数）
  doc://{doc_id}/outline  ── 文档大纲（标题树）
  doc://{doc_id}/chunk/{n}── 第 n 个 chunk

支持格式：PDF(pymupdf) / DOCX(python-docx) / HTML(trafilatura) / MD / 代码(tree-sitter)
```

---

#### `memory-rag` —— 向量记忆

```python
# 传输：stdio  存储：ChromaDB（collection: "memory"）

Tools:
  remember(content: str, tags: list[str] = [], importance: 1-5 = 3) -> MemoryId
    # 内容向量化存储，importance 影响 compaction 时的保留优先级

  recall(query: str, top_k: int = 5, min_score: float = 0.7) -> list[MemorySummary]
    # 语义检索，返回摘要（每条 ≤ 100 tokens）+ memory_id
    # Agent 需要全文时用 get_memory(id)

  get_memory(memory_id: str) -> str
    # 读取完整记忆内容

  forget(memory_id: str) -> bool
  list_memories(tag: str = None, limit: int = 20) -> list[MemoryMeta]

Resources:
  memory://recent       ── 最近 10 条记忆的元数据
  memory://important    ── importance >= 4 的记忆
  memory://tags         ── 所有 tag 列表
```

---

#### `code-index` —— 代码库索引

```python
# 传输：stdio  存储：ChromaDB（collection: "codebase"）

Tools:
  index(root_dir: str, extensions: list[str] = None) -> IndexStats
    # 增量索引（只索引新增/修改的文件）
    # 使用 tree-sitter 按函数/类边界分割，保留语义完整性

  search(query: str, top_k: int = 5, file_filter: str = None) -> list[CodeChunkSummary]
    # 语义搜索，返回：文件路径 + 行号 + 函数名 + 2行摘要
    # 完整代码通过 read_file 工具获取

  find_symbol(name: str, type: "function"|"class"|"variable" = None) -> list[SymbolLocation]
    # 精确符号查找（比向量检索更准）

Resources:
  code://stats          ── 索引统计（文件数、symbol 数、最后更新时间）
  code://files          ── 已索引文件列表
```

### 6.3 MCP 工具命名规范

```
mcp__{server_name}__{tool_name}

示例：
  mcp__websearch__search
  mcp__websearch__fetch_page
  mcp__docparse__parse
  mcp__docparse__chunk_and_index
  mcp__memory_rag__recall
  mcp__memory_rag__remember
  mcp__code_index__search
  mcp__code_index__find_symbol
```

### 6.4 工具设计原则（对所有工具有效）

1. **Token-efficient**：默认返回摘要，完整内容存 artifact，按需读取
2. **Non-overlapping**：工具间功能互斥，不能出现两个工具做同一件事
3. **Self-contained**：工具的目的从名字就能看懂，不需要上下文才能理解
4. **Robust**：异常情况返回自然语言错误说明，而不是 stack trace

---

## 七、Agent Loop（四级进化）

### Level 1：简单对话

```
user_message
    │
    ▼
context.build()           ── 最小化 context（只有 cinstr+cmem索引+cquery）
    │
    ▼
llm.stream_chat()         ── 流式调用
    │
    ▼
yield text_chunk → WS → Frontend   （实时流式显示）
    │
    ▼
context.append_assistant(full_response)
```

### Level 2：工具循环

```
user_message
    │
    ▼
context.build()
    │
    ▼
┌─── llm.stream_chat(tools=registry.get_schemas()) ────────────┐
│                                                               │
│  text_chunk  → yield to frontend（实时显示）                  │
│                                                               │
│  tool_call   ──────────────────────────────────────────────┐ │
│                                                            ▼ │
│                  result = tool_registry.execute(name, args)  │
│                                                            │ │
│                  if result.artifact_id:                    │ │
│                      artifact_store.save(result)           │ │
│                      context.append_tool_result(summary)   │ │
│                  else:                                     │ │
│                      context.append_tool_result(result)    │ │
│                                                            │ │
│                  yield tool_result_event → frontend        │ │
│                  continue loop ────────────────────────────┘ │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
yield done
```

### Level 3：审批循环

在 Level 2 基础上，敏感操作插入人工审批节点：

```
tool_call 到达
    │
    ▼
permission.check(tool_name, args)
    │
    ├── AUTO → 直接执行（read_file / list_files / recall 等）
    │
    ├── CONFIRM → yield approval_request
    │                  │
    │            等待 WS: approve / reject / reject_with_guidance
    │                  │
    │             approve → 执行
    │             reject  → 将拒绝原因加入 context，Agent 重新规划
    │
    └── DIFF_REVIEW → 生成 unified diff → yield diff_review
                           │
                     用户 approve/reject/edit
```

### Level 4：完整 Agent Loop

```python
async def run(user_message: str, state: AgentState):
    # ── 会话前置处理 ──
    memory.load_index()                        # 加载 MEMORY.md 索引（轻量）
    active_skills = skills.auto_detect(user_message)
    skills.load(active_skills)                 # 加载匹配 Skill 的 Layer 2

    # ── 被动 RAG（低相关性阈值，只注入高度相关的背景知识）──
    background = rag.retrieve(user_message, top_k=3, min_score=0.82)
    state.retrieved_chunks = background

    # ── 构建 Context & 进入工具循环 ──
    loop_count = 0
    same_tool_calls = {}   # 停滞检测

    while True:
        # 终止条件检查
        if loop_count > MAX_ITERATIONS:           # 预算终止
            yield error_event("超出最大迭代次数")
            break
        if context.token_usage > TOKEN_BUDGET * 0.75:
            context.compact()                     # Compaction
        if is_stagnant(same_tool_calls):          # 停滞终止
            yield error_event("检测到循环，停止执行")
            break

        messages = context.build(user_message, state)
        event = await llm.stream_chat(messages, tools=registry.get_schemas())

        if event.type == "done":                  # 正常终止
            break
        if event.type == "tool_call":
            # Level 3 审批逻辑...
            result = await execute_with_permission(event)
            context.append_tool_result(result)
            same_tool_calls[event.name] = same_tool_calls.get(event.name, 0) + 1
            loop_count += 1

    # ── 会话后置处理 ──
    await memory.save_session_summary(context.get_summary())   # 更新长期记忆
```

### 终止条件（四种）

| 类型 | 触发条件 | 处理 |
|------|---------|-----|
| **正常终止** | Agent 完成任务，不再调用工具 | 正常结束，更新记忆 |
| **预算终止** | loop_count > 30 或 token > 0.95 * budget | 告知用户，存当前进度到 state |
| **停滞终止** | 同一工具 + 同一参数调用 ≥ 3 次 | 告知用户卡住，请求指导 |
| **异常终止** | LLM API 报错 / 工具 panic | 捕获 + 自然语言错误 + 恢复机制 |

---

## 八、工具系统

### 8.1 工具注册中心

```python
class ToolRegistry:
    """统一管理内置工具 + MCP 动态注册工具"""

    def register(self, tool: BaseTool) -> None: ...

    def get_schemas(self, budget: int = 6000) -> list[dict]:
        """返回工具 JSON Schema 列表，按重要性排序，控制总 token 数"""
        # 如果工具太多，先返回核心工具的完整 schema，其余只给 name+一行描述
        ...

    async def execute(self, name: str, args: dict) -> ToolResult: ...
```

### 8.2 内置工具（含输出规范）

| 工具名 | 说明 | 输出上限 | 权限 |
|--------|------|---------|------|
| `read_file` | 读文件内容 | 2K tokens（超出存 artifact）| AUTO |
| `list_files` | 列目录 | 100 条（超出分页）| AUTO |
| `grep_files` | 正则搜索 | 50 条匹配行 | AUTO |
| `write_file` | 写文件 | — | DIFF_REVIEW |
| `edit_file` | 精确编辑（unified diff patch）| — | DIFF_REVIEW |
| `run_command` | 执行命令 | 500 tokens stdout（超出存 artifact）| CONFIRM |
| `ask_user` | 主动提问 | — | AUTO |
| `read_memory` | 读取具体记忆文件 | 文件全文 | AUTO |
| `save_memory` | 写入/更新记忆文件 | — | AUTO |
| `read_artifact` | 读取 artifact 全文 | 完整内容 | AUTO |
| `load_skill` | 加载 Skill Layer 2 | — | AUTO |
| `web_fetch` | 获取网页内容 | 1K tokens 摘要 + artifact | CONFIRM |

### 8.3 权限系统

```python
class PermissionLevel(Enum):
    AUTO = "auto"           # 自动执行
    CONFIRM = "confirm"     # 展示参数，用户确认
    DIFF_REVIEW = "diff"    # 展示 diff，用户审批
    ALWAYS_DENY = "deny"    # 永远拒绝

# settings.json 配置
{
  "permissions": {
    "auto_allow": ["read_file", "list_files", "grep_files", "ask_user", "read_memory", "read_artifact", "load_skill", "mcp__memory_rag__*", "mcp__code_index__search"],
    "require_confirm": ["run_command", "web_fetch", "mcp__websearch__*"],
    "require_diff_review": ["write_file", "edit_file"],
    "always_deny": [],
    "path_allowlist": ["./src", "./tests", "./backend", "./frontend"],
    "path_denylist": [".env", "*.key", "*.pem", "secrets/"]
  }
}
```

---

## 九、Artifact Store（消息层 vs 产物层分离）

这是 design_principle.md 中"双层架构"在 MiniCode 中的实现。

```
消息层（Message Layer）：
  在 context window 中流转
  内容：摘要、状态、决策、工具结果的精简版
  限制：每条工具结果 ≤ 500 tokens

产物层（Artifact Layer）：
  在 Artifact Store 中存储（内存字典或本地文件）
  内容：工具原始输出、搜索结果全文、解析文档、代码片段全文
  访问：Agent 调用 read_artifact(artifact_id) 按需读取
```

```python
class ArtifactStore:
    """会话级产物存储，会话结束后清理（重要内容已经进了长期记忆）"""

    def save(self, content: str, source: str, type: str) -> str:
        """存储大内容，返回 artifact_id"""
        ...

    def get(self, artifact_id: str) -> str:
        """读取完整内容"""
        ...

    def get_preview(self, artifact_id: str, lines: int = 5) -> str:
        """读取预览，用于 context 中的引用描述"""
        ...
```

---

## 十、WebSocket 通信协议

### 前端 → 后端（消息层，保持轻量）

```typescript
{ type: "user_message",  content: string, session_id?: string }
{ type: "approval",      tool_call_id: string, action: "approve"|"reject"|"reject_with_guidance", guidance?: string }
{ type: "interrupt" }    // 中断当前生成
{ type: "load_skill",    skill_name: string }
```

### 后端 → 前端

```typescript
{ type: "text_chunk",       content: string }
{ type: "tool_call",        id: string, name: string, args: object }
{ type: "tool_result",      id: string, summary: string, artifact_id?: string }
{ type: "approval_request", tool_call_id: string, tool_name: string, args: object, diff?: string }
{ type: "skill_activated",  skill_name: string, description: string }
{ type: "context_compacted",summary: string }     // compaction 发生时通知前端
{ type: "done",             usage: { input_tokens: number, output_tokens: number } }
{ type: "mcp_status",       servers: MCPServerStatus[] }
{ type: "error",            message: string, recoverable: boolean, error_type: "budget"|"stagnant"|"api"|"permission" }
```

---

## 十一、完整项目结构

```
MiniCode/
├── backend/
│   ├── main.py                    # FastAPI 入口，WebSocket 端点，会话管理
│   ├── config.py                  # pydantic-settings，.env 读取
│   │
│   ├── agent/
│   │   ├── loop.py                # Agent Loop 四级进化 + 四种终止条件
│   │   ├── context.py             # Context 构建（渐进式披露）+ Compaction
│   │   ├── state.py               # AgentState：任务树、artifact 引用、active skills
│   │   └── message.py             # 消息数据模型（与 API 格式互转）
│   │
│   ├── llm/
│   │   ├── base.py                # LLMAdapter 抽象类 + StreamEvent 类型定义
│   │   ├── openai_adapter.py      # OpenAI / 兼容 API（stream + tool_call）
│   │   └── anthropic_adapter.py   # Anthropic Claude（stream + tool_use）
│   │
│   ├── tools/
│   │   ├── registry.py            # ToolRegistry：注册、schema 生成、路由执行
│   │   ├── base.py                # BaseTool + ToolResult（含 artifact 支持）
│   │   ├── file_tools.py          # read_file / write_file / edit_file / list_files
│   │   ├── search_tools.py        # grep_files
│   │   ├── command_tool.py        # run_command（subprocess，超时控制）
│   │   └── agent_tools.py         # ask_user / read_memory / save_memory / read_artifact / load_skill / web_fetch
│   │
│   ├── mcp/
│   │   ├── client.py              # MCP 客户端（stdio subprocess + HTTP SSE）
│   │   ├── manager.py             # Server 生命周期（启动/停止/重启/健康检查）
│   │   ├── registry.py            # 动态工具注册，mcp__{server}__{tool} 命名
│   │   └── servers/
│   │       ├── websearch.py       # 网页搜索（duckduckgo + trafilatura）
│   │       ├── docparse.py        # 文档解析（pymupdf + python-docx + tree-sitter）
│   │       ├── memory_rag.py      # 向量记忆（ChromaDB，collection: memory）
│   │       └── code_index.py      # 代码索引（tree-sitter + ChromaDB，collection: codebase）
│   │
│   ├── memory/
│   │   ├── file_memory.py         # MEMORY.md 索引读写 + 具体文件读写
│   │   ├── vector_memory.py       # ChromaDB 封装（embed + upsert + query）
│   │   └── manager.py             # 统一接口：load_index / read_file / save_file / recall
│   │
│   ├── rag/
│   │   ├── embedder.py            # Embedding（OpenAI text-embedding-3-small / bge-m3）
│   │   ├── chunker.py             # 分块策略（通用/代码/对话 三种模式）
│   │   ├── retriever.py           # 向量检索 + 相关性过滤
│   │   └── pipeline.py            # 被动 RAG 流水线（Context 构建时调用）
│   │
│   ├── artifact/
│   │   └── store.py               # ArtifactStore：会话级大内容存储（内存+可选落盘）
│   │
│   ├── skills/
│   │   ├── loader.py              # SKILL.md 发现（三级目录）+ Layer 1/2/3 解析
│   │   ├── manager.py             # 激活/停用/列出/冲突检测
│   │   └── executor.py            # Skill 注入 Context（Layer 2 内容拼入 system）
│   │
│   ├── permissions/
│   │   ├── checker.py             # 工具调用权限检查（AUTO/CONFIRM/DIFF/DENY）
│   │   └── review.py              # Diff 生成（difflib unified_diff）
│   │
│   └── ws/
│       └── handler.py             # WebSocket 消息路由、会话 ID 管理、事件序列化
│
├── frontend/
│   └── src/
│       ├── App.tsx
│       ├── types.ts                # WS 协议的 TypeScript 类型（与后端协议文档同步）
│       ├── components/
│       │   ├── ChatPanel.tsx       # 主聊天面板
│       │   ├── MessageBubble.tsx   # 消息气泡（Markdown + 代码高亮）
│       │   ├── ToolCallView.tsx    # 工具调用可视化（折叠/展开，含 artifact 预览）
│       │   ├── DiffReview.tsx      # unified diff 展示 + approve/reject
│       │   ├── MCPStatus.tsx       # MCP Server 连接状态面板
│       │   └── SkillSelector.tsx   # Skills 选择器（显示 Layer 1 描述）
│       ├── hooks/
│       │   └── useWebSocket.ts     # WS 连接 + 断线重连 + 消息队列
│       └── stores/
│           └── chatStore.ts        # Zustand：消息列表、工具状态、skill 状态
│
├── skills/
│   ├── frontend-dev/SKILL.md
│   ├── code-review/SKILL.md
│   ├── git-workflow/SKILL.md
│   └── debug-mode/SKILL.md
│
├── data/                           # gitignore
│   ├── chroma/                     # ChromaDB 数据
│   ├── memory/                     # 文件记忆
│   └── artifacts/                  # 可选：artifact 落盘
│
├── .mcp.json                       # MCP Server 启动配置
├── settings.json                   # 权限/模型/行为全局配置
├── .env.example
├── requirements.txt
└── DESIGN.md
```

---

## 十二、开发路线图

| 阶段 | 核心交付 | 学到的关键概念 |
|------|---------|---------------|
| **Phase 1** | FastAPI + WebSocket + LLM 适配层 + 最小化 Context + 流式对话 | LLM 抽象、流式传输、Context 最小化原则 |
| **Phase 2** | ToolRegistry + 内置工具 + Tool-Calling Loop + ArtifactStore + 终止条件 | Tool-Use 协议、Artifact 模式、停滞检测 |
| **Phase 3** | 权限系统 + Diff Review + 审批流 + AgentState | 安全边界、人机协作、状态管理 |
| **Phase 4** | MCP 客户端 + 4 个内置 Server（Token-efficient 输出）| MCP 协议、stdio/HTTP、动态注册、工具设计原则 |
| **Phase 5** | Skills 三层加载 + 完整 Context Engineering 流水线 + Compaction | Progressive Disclosure、Token 预算、Prompt 工程 |
| **Phase 6** | 长期记忆 + 双模式 RAG + Artifact/Message 双层 + 完整 Level 4 Loop | 向量检索、记忆管理、信息流架构 |

---

## 十三、关键设计决策

### Q1: Skills vs MCP 的边界？

- **Skills** = `cinstr` 层扩展，改变 Agent 的"思维方式"，零代码，Prompt 注入
- **MCP** = `cknow`/`ctools` 层扩展，提供新工具和数据，需要写代码
- Skill 可以在 `mcp_required` 声明自己依赖某个 MCP，两者互补不重叠

### Q2: RAG 做成 MCP Server 有什么好处？

1. Agent 可以**主动** `recall` / `remember`（Agentic RAG）
2. Context 流水线可以**被动**静默检索（被动 RAG）
3. 两条路径共用同一套 ChromaDB 存储，数据不重复
4. MCP Server 独立部署，可被其他 Agent 复用

### Q3: Compaction 时该保留什么？

优先级（从高到低）：
1. 用户明确的约束/指令（"不要 commit"、"只用 TypeScript"）
2. 含工具调用和结果的轮次（有事实依据）
3. 关键决策点（Agent 改变了方向的轮次）
4. 最近 15 轮（无论内容）

### Q4: 工具太多会不会让 LLM 选错？

是。缓解策略：
1. `get_schemas(budget)` 按 token 预算动态裁剪，工具太多时只给 name+一行描述
2. MCP 工具按 server 分组，每次只暴露**已连接**的 MCP server 工具
3. Skill 激活时补充该 Skill 的工具使用指导（哪些工具用于哪个步骤）

### Q5: 多个 Skills 冲突怎么处理？

- SKILL.md 在 `conflicts` 字段声明冲突的 Skill 名
- Manager 检测到冲突：优先保留最近激活的，停用旧的
- 合并注入时，有冲突规则标注"以本 Skill 为准"

---

## 十四、Anthropic 设计原则融合

> 来源：Anthropic "Building Effective Agents" + 官方 Tool Use / Multi-Agent 文档

### 14.1 简单性优先决策树

Anthropic 的第一原则：**只在简单方案不够用时才加复杂性**。

```
用户的请求
    │
    ▼
能否用单次 LLM 调用 + 检索解决？
    ├── 是 → 直接返回（不需要 Agent）
    │
    └── 否
        │
        ▼
    步骤是否固定可预测？
        ├── 是 → 用工作流（Workflow）：Prompt Chaining / Routing
        │
        └── 否
            │
            ▼
        是否需要动态决策 + 工具循环？
            ├── 是 → 用 Agent Loop（L2/L3）
            │
            └── 是否需要并行子任务？
                    └── 是 → 用 Orchestrator-Workers（L4+）
```

**MiniCode 中的映射**：

| Anthropic 模式 | 触发条件 | MiniCode 实现 |
|--------------|---------|-------------|
| Prompt Chaining | 固定多步任务（文档→摘要→翻译）| 多个顺序工具调用 |
| Routing | 根据请求类型选不同 Skill | Skills auto_detect + 条件注入 |
| Parallelization | 独立子问题可并行 | `asyncio.gather` 并行工具调用 |
| Orchestrator-Workers | 开放式复杂任务（大型重构）| Agent 调用 `ask_user` 拆解任务，子任务顺序/并行执行 |
| Evaluator-Optimizer | 需要迭代优化的输出（代码质量）| Agent 自调用 + `grep_files` 验证结果 |
| Autonomous Agent | 开放探索型任务 | Level 4 完整 Loop |

### 14.2 ACI：Agent-Computer Interface 设计原则

Anthropic 将工具设计提升到与 HCI 同等重要的地位。MiniCode 的每个工具 schema 必须满足：

```
工具 schema 检查清单：

□ 名称自描述：仅看名称就知道做什么（read_file ✓，process_data ✗）
□ 参数无歧义：每个参数只有一种理解方式
□ 描述含示例：description 字段包含至少一个使用示例
□ 边界情况说明：文件不存在/超大文件/权限不足时会怎样
□ 输出格式明确：返回什么结构，token 上限是多少
□ 防误用设计（poka-yoke）：不可能的参数组合在 schema 层面拦截
□ strict: true：关键工具开启严格 schema，保证参数格式精确匹配
```

**工具文档示例（好 vs 差）**：

```python
# ✗ 差：描述模糊，无示例，无边界说明
{
    "name": "edit_file",
    "description": "编辑文件内容"
}

# ✓ 好：描述精确，含示例，含边界说明
{
    "name": "edit_file",
    "description": (
        "对文件进行精确的字符串替换。"
        "old_string 必须是文件中唯一存在的字符串，否则报错。"
        "示例：将函数名从 get_user 改为 fetch_user。"
        "注意：不适用于大段重写，大段修改请用 write_file。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "绝对路径，如 /c/Desktop/MiniCode/backend/main.py"},
            "old_string": {"type": "string", "description": "要替换的原始字符串（必须在文件中唯一存在）"},
            "new_string": {"type": "string", "description": "替换后的新字符串"}
        },
        "required": ["file_path", "old_string", "new_string"]
    },
    "strict": true
}
```

### 14.3 工具数量控制

Anthropic 明确指出：**工具太多会让 Agent 选错工具，甚至选 SEO 优化的内容而非权威来源**。

```
工具集管理策略：

全局工具（始终可见）：
  read_file / list_files / grep_files / ask_user / read_artifact  ← 5个，核心认知工具

条件工具（Skill 激活时追加）：
  write_file / edit_file       ← 代码 Skill 激活
  run_command                  ← 需要命令执行时
  web_fetch                    ← 需要网络时

MCP 工具（Server 连接后追加）：
  mcp__websearch__*            ← websearch server 启动后
  mcp__memory_rag__*           ← memory-rag server 启动后

原则：每次 LLM 调用的工具 schema 总量 ≤ 6K tokens
      工具数量超过 15 个时，使用摘要格式替代完整 schema
```

### 14.4 多 Agent 信任层级（为 Phase 6+ 扩展准备）

当 MiniCode 扩展到支持 Orchestrator-Workers 模式时：

```
信任层级：

Orchestrator（主 Agent）：
  - 接受用户指令，分解为子任务
  - 维护 AgentState（任务树 + artifact 引用）
  - 不直接执行细节，只协调和综合
  - 使用 extended thinking 做规划

Worker（子 Agent）：
  - 在独立 context window 中执行单一子任务
  - 接受结构化 handoff packet（任务说明+边界+输出格式）
  - 返回结构化摘要（结论+证据+置信度+遗留问题）
  - 不需要知道其他 Worker 在做什么

Handoff Packet 格式：
  {
    "task": "找出 backend/agent/loop.py 中的所有异步问题",
    "boundary": "只分析 loop.py，不涉及其他文件",
    "inputs": ["artifact_001"],          // 可用输入
    "output_format": "问题列表 + 行号 + 严重程度",
    "done_when": "所有异步调用都已检查",
    "stop_conditions": "最多 10 轮工具调用"
  }
```

### 14.5 可靠性与安全

来自 Anthropic 生产经验：

```
1. 渐进测试：从 20 个真实场景查询开始评估，而非等大数据集
   → MiniCode 在 Phase 2 起建立 test_cases/ 目录

2. 检查点恢复：长任务每完成一个阶段，将进度写入 AgentState 持久化
   → run_command 等危险操作前先保存当前 state

3. 优雅降级：工具失败时返回自然语言错误而非 panic，让 Agent 自行调整
   → ToolResult.error 字段：人类可读的错误描述 + 建议的替代方案

4. Token 用量是成功率最强预测因子（解释 80% 方差）
   → 监控每次会话的 token 消耗，识别 token 异常高的任务模式

5. 明确迭代上限：Agent 不能无限循环
   → MAX_ITERATIONS = 30，接近时通知用户，询问是否继续

6. 文件系统 > 直接传递：大量信息通过 Artifact 传递，而非塞进对话
   → 贯穿整个 ArtifactStore 设计
```

---

## 十五、UI 设计规范

### 15.1 整体布局

```
┌──────────────────────────────────────────────────────────────────┐
│  ◆ MiniCode                              [⚡Skills] [☁MCP] [⚙]  │  ← Header (48px)
├────────────────────────────────────┬─────────────────────────────┤
│                                    │                              │
│         主聊天区                    │       上下文侧边栏            │
│         (flex: 1)                  │       (280px, 可折叠)         │
│                                    │  ┌──────────────────────┐   │
│  ┌──────────────────────────────┐  │  │ Skills               │   │
│  │ [用户消息气泡]                 │  │  │ ● frontend-dev  ×   │   │
│  └──────────────────────────────┘  │  │ + 添加 Skill         │   │
│                                    │  └──────────────────────┘   │
│  ◆                                 │  ┌──────────────────────┐   │
│  [助手回复 · 流式渲染]              │  │ MCP Servers          │   │
│  └─ 工具调用卡片（可折叠）          │  │ ● websearch  connected│   │
│  └─ Diff 审批卡片（阻塞）          │  │ ● docparse   connected│   │
│                                    │  │ ○ code-index offline  │   │
│                                    │  └──────────────────────┘   │
│                                    │  ┌──────────────────────┐   │
│                                    │  │ Context Budget        │   │
│                                    │  │ ████████░░  68K/128K  │   │
│                                    │  │ sys 2K  rag 6K        │   │
│                                    │  │ hist 52K  tools 8K    │   │
│                                    │  └──────────────────────┘   │
│                                    │  ┌──────────────────────┐   │
│                                    │  │ Memory Index         │   │
│                                    │  │ • user_profile       │   │
│                                    │  │ • project_context    │   │
│                                    │  └──────────────────────┘   │
├────────────────────────────────────┴─────────────────────────────┤
│  [⚡] [📎]  输入框（auto-resize，最大 6 行）         [发送 ↵]     │  ← Input (min 64px)
└──────────────────────────────────────────────────────────────────┘
```

响应式：侧边栏在 < 1024px 时默认折叠，通过右上角按钮展开。

### 15.2 设计系统

#### 颜色（Dark Theme）

```css
:root {
  /* 背景层级 */
  --bg-base:     #0d0d0d;   /* 最底层背景 */
  --bg-surface:  #171717;   /* 卡片/面板背景 */
  --bg-elevated: #222222;   /* 悬浮元素/输入框 */
  --bg-overlay:  #2a2a2a;   /* Tooltip/Dropdown */

  /* 边框 */
  --border:      #2e2e2e;
  --border-subtle: #1e1e1e;

  /* 文字 */
  --text-primary:   #e8e8e8;
  --text-secondary: #8c8c8c;
  --text-muted:     #4a4a4a;
  --text-code:      #d4d4d4;

  /* 强调色 */
  --accent:      #cc785c;   /* Anthropic 橙 - 主操作 */
  --accent-dim:  #7c3f2a;   /* 强调色暗版 */
  --link:        #4a9eff;   /* 链接/次要操作 */

  /* 状态色 */
  --success:  #52c41a;
  --warning:  #faad14;
  --error:    #ff4d4f;
  --info:     #1890ff;

  /* 工具调用专用 */
  --tool-bg:      #1a1a2e;   /* 工具调用卡片背景（蓝调） */
  --tool-border:  #2a2a4e;
  --approval-bg:  #2a1a0a;   /* 审批卡片背景（橙调） */
  --approval-border: #4a2a0a;
  --diff-add:     #0d2611;   /* diff 新增行背景 */
  --diff-remove:  #2d0d0d;   /* diff 删除行背景 */
}
```

#### 字体

```css
--font-ui:   "Inter", "PingFang SC", system-ui, sans-serif;
--font-code: "JetBrains Mono", "Fira Code", "Consolas", monospace;

--text-xs:   12px;  /* 元数据、标签 */
--text-sm:   13px;  /* 次要文字 */
--text-base: 14px;  /* 主体文字 */
--text-md:   15px;  /* 消息正文 */
--text-lg:   16px;  /* 标题 */

--leading-normal: 1.5;
--leading-relaxed: 1.7;  /* Markdown 正文 */
```

#### 间距

```css
--space-1: 4px;   --space-2: 8px;   --space-3: 12px;
--space-4: 16px;  --space-5: 20px;  --space-6: 24px;
--space-8: 32px;

--radius-sm: 6px;
--radius-md: 10px;
--radius-lg: 14px;
--radius-xl: 20px;
```

### 15.3 消息气泡

```
用户消息：
┌─────────────────────────────────────────────┐
│                              你的消息内容      │  背景: --bg-elevated
│                             2025-04-15 14:30  │  圆角: --radius-xl，右下角 4px
└─────────────────────────────────────────────┘
                                          [头像]  字母缩写或头像图片

助手消息：
[◆]  ← MiniCode logo (24px)
     MiniCode
     助手回复内容，支持 Markdown：
     
     **加粗** `行内代码` [链接](#)
     
     ```python
     # 代码块：JetBrains Mono 13px
     # 含语言标签 + 复制按钮
     def hello(): pass
     ```
     
     14:31 · 1.2k tokens                 ← 底部元数据（--text-secondary）
```

### 15.4 工具调用卡片

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┌─────────────────────────────────────────────┐
│  ▶ read_file                        ✓ 23ms  │  ← 标题行（--tool-bg）
│    backend/agent/loop.py                     │
├─────────────────────────────────────────────┤
│  ˅ 展开结果                                  │  ← 折叠触发区
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─      │
│  ```python                                   │
│  async def run(self, msg):                   │  ← 结果预览（最多 10 行）
│      ...                                     │
│  ```                                         │
│  [查看完整 artifact →]                       │  ← artifact 引用
└─────────────────────────────────────────────┘

状态图标：
  ⟳（旋转）→ 执行中（--accent 色）
  ✓         → 成功（--success 色）
  ✗         → 失败（--error 色）
  ⏸         → 等待审批（--warning 色）
```

### 15.5 审批卡片（Diff Review）

```
┌─────────────────────────────────────────────────────────┐
│  ⚠  需要审批：edit_file                                   │  ← 标题（--approval-bg）
│  路径：backend/agent/loop.py                              │
├─────────────────────────────────────────────────────────┤
│  @@ -42,7 +42,9 @@ async def run(self, msg: str):        │
│                                                           │
│     42 │   while True:                                   │
│  -  43 │       if loop_count > 20:                       │  ← 红色背景
│  +  43 │       if loop_count > MAX_ITERATIONS:           │  ← 绿色背景
│  +  44 │           logger.warning("max iter reached")    │
│     45 │           break                                  │
│                                                           │
├─────────────────────────────────────────────────────────┤
│  [✓ 批准]      [✗ 拒绝]      [✎ 带意见拒绝]               │
│                              └── 弹出文本框让用户输入意见  │
└─────────────────────────────────────────────────────────┘

Diff 颜色：
  新增行背景：--diff-add    (#0d2611)，行号：--success
  删除行背景：--diff-remove (#2d0d0d)，行号：--error
  上下文行：--bg-surface，行号：--text-muted
```

### 15.6 输入区

```
┌──────────────────────────────────────────────────────────────┐
│  [⚡][📎]  发送消息，或用 /skill-name 激活 Skill...            │
│                                                              │
│           （auto-resize，最大 6 行，Enter 发送，             │
│            Shift+Enter 换行）                                │
│                                                    [→ 发送]  │
└──────────────────────────────────────────────────────────────┘

流式生成中时：
┌──────────────────────────────────────────────────────────────┐
│  正在生成... ████████░░░░░░░░░░░░░░         [■ 中断生成]       │
└──────────────────────────────────────────────────────────────┘

按钮说明：
  [⚡] → 打开 Skills 命令面板（搜索+激活 Skill）
  [📎] → 上传文件（触发 docparse 解析+入库）
  [→]  → 发送（Enter 快捷键）
  [■]  → 中断（仅流式生成时出现）
```

### 15.7 Skills 命令面板

输入 `/` 或点击 `[⚡]` 时弹出：

```
┌────────────────────────────────────────┐
│  ⚡ Skills                              │
│  ─────────────────────────────────────  │
│  🔍 搜索 Skill...                       │
│  ─────────────────────────────────────  │
│  ● frontend-dev     ← 已激活，带圆点    │
│    React 18 + TS + Tailwind 专家模式    │
│                                         │
│  ○ code-review                          │
│    代码审查模式，关注性能与安全          │
│                                         │
│  ○ debug-mode                           │
│    系统化 debug 流程                    │
│                                         │
│  ○ git-workflow                         │
│    Git 操作规范与 commit 格式           │
└────────────────────────────────────────┘
点击激活/停用；已激活的 Skill 显示橙色圆点
```

### 15.8 Context Budget 可视化

```
┌────────────────────────────────────┐
│ Context Budget                     │
│                                    │
│  ████████████░░░░░░░  68K / 128K   │  ← 总进度条
│                                    │
│  System   ██░         2.1K         │
│  Skills   ███░        3.2K         │
│  RAG      ████░       5.8K         │
│  History  ████████░  52.4K         │
│  Tools    ████░       4.5K         │
│                                    │
│  [⚡ 触发压缩]  压缩阈值：75%       │
└────────────────────────────────────┘

进度条颜色：
  0-60%:  --success（绿）
  60-80%: --warning（黄）
  80%+:   --error（红）
  触发 compaction 后：动画闪烁 → 重置到较低值，显示"已压缩"提示
```

### 15.9 MCP 状态面板

```
┌────────────────────────────────────┐
│ MCP Servers                        │
│                                    │
│  ● websearch          connected    │  ← 绿点
│    2 tools available                │
│                                    │
│  ● docparse           connected    │
│    4 tools · 3 resources           │
│                                    │
│  ○ code-index         starting...  │  ← 黄点 + spinner
│                                    │
│  ✗ memory-rag         error        │  ← 红叉
│    [重试连接]                       │
└────────────────────────────────────┘
```

### 15.10 交互状态规范

| 状态 | 表现 |
|------|------|
| 流式生成中 | 光标闪烁（`|`），input 区域禁用并显示进度条 |
| 工具执行中 | 工具卡片显示 spinner + 执行时间计数 |
| 等待审批 | 审批卡片阻塞（半透明遮罩），其他 UI 不可操作 |
| Compaction 发生 | 侧边栏 Context 面板闪烁橙色，显示"对话已压缩"toast |
| MCP 断连 | header 中 MCP 图标变红，面板中对应 server 显示错误 |
| Skill 激活 | Input 区域顶部出现橙色 Skill 标签，可点击 × 取消 |
| 错误（Agent 停滞）| 消息流末尾插入红色错误卡片，提供"重试"/"换个方式描述"选项 |

### 15.11 前端组件树

```
App
├── Header
│   ├── Logo
│   ├── SkillBadges (已激活的 Skill 标签)
│   └── IconButtons (Skills / MCP / Settings)
│
├── MainLayout
│   ├── ChatPanel (flex: 1)
│   │   ├── MessageList (overflow-y: auto)
│   │   │   ├── UserMessage
│   │   │   └── AssistantMessage
│   │   │       ├── MarkdownRenderer
│   │   │       ├── ToolCallCard (可折叠)
│   │   │       │   └── ArtifactPreview
│   │   │       └── ApprovalCard (阻塞)
│   │   │           ├── DiffViewer
│   │   │           └── ApprovalButtons
│   │   └── ScrollAnchor (auto-scroll to bottom)
│   │
│   └── ContextSidebar (280px, 可折叠)
│       ├── ActiveSkillsPanel
│       ├── MCPStatusPanel
│       ├── ContextBudgetPanel
│       └── MemoryIndexPanel
│
└── InputArea (fixed bottom)
    ├── SkillBadgeRow (已激活的 Skill)
    ├── TextareaInput
    ├── AttachButton
    └── SendButton / InterruptButton
```

### 15.12 关键交互流程

**发送消息 → 流式响应**：
```
用户点击发送
  → WS 发送 user_message
  → 立即在 MessageList 追加 UserMessage（乐观更新）
  → 立即追加空的 AssistantMessage（显示 spinner）
  → 收到 text_chunk → 追加到当前 AssistantMessage（流式渲染）
  → 收到 tool_call  → 在 AssistantMessage 内插入 ToolCallCard（loading 状态）
  → 收到 tool_result→ 更新对应 ToolCallCard（done + 结果预览）
  → 收到 approval_request → 插入 ApprovalCard，全局 UI 半禁用
  → 用户 approve/reject → WS 发送 approval → UI 解禁继续
  → 收到 done → AssistantMessage 完成，显示 token 用量
```

---

## 十六、总结：设计哲学一句话

> **用最少的 token，在正确的时机，把正确的信息交给模型；**
> **然后收起双手，只在必要时介入。**

这一句话覆盖了：
- Context Engineering 的信息密度原则
- Progressive Disclosure 的渐进展开原则
- Anthropic "Simplicity First" 的复杂性控制原则
- 人机协作的审批设计原则

---

*文档版本 v3.0 | 融合 Anthropic 设计原则 + UI 规范*
