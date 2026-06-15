# Workflow 示例脚本集合

## 示例 1: 深度代码审查 Workflow

```python
export const meta = {
    name: 'deep-code-review',
    description: '深度代码审查 - 并行查找问题 + 对抗性验证',
    phases: [
        { title: 'Find Issues', detail: '并行扫描所有文件' },
        { title: 'Verify', detail: '对抗性验证每个发现' },
    ],
}

# Schema 定义
BUGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string"},
                    "description": {"type": "string"}
                }
            }
        }
    },
    "required": ["findings"]
}

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "real": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"}
    },
    "required": ["real"]
}

# Phase 1: 并行查找问题
phase('Find Issues')
log('Starting bug scan across changed files...')

bugs = await agent(
    "Find all potential bugs, security issues, and logic errors in the changed files. "
    "Return a structured list of findings.",
    schema=BUGS_SCHEMA,
    label='bug-scanner'
)

if not bugs or not bugs.get('findings'):
    log('No issues found')
    return {"confirmed_bugs": []}

log(f"Found {len(bugs['findings'])} potential issues")

# Phase 2: 对抗性验证
phase('Verify')
log('Adversarially verifying each finding...')

# 并行验证所有发现
verified = await parallel([
    lambda f=finding: agent(
        f"Try to REFUTE this claim: {f['title']} in {f['file']}. "
        f"Description: {f['description']}. "
        f"Default to real=False if uncertain.",
        schema=VERDICT_SCHEMA,
        label=f"verify-{f['file']}"
    )
    for finding in bugs['findings']
])

# 筛选出确认的 Bug
confirmed = [
    finding
    for finding, verdict in zip(bugs['findings'], verified)
    if verdict and verdict.get('real', False)
]

log(f"{len(confirmed)}/{len(bugs['findings'])} issues confirmed")

return {"confirmed_bugs": confirmed, "total_found": len(bugs['findings'])}
```

---

## 示例 2: Pipeline 模式 - 逐文件审查

```python
export const meta = {
    name: 'file-by-file-review',
    description: 'Pipeline 模式 - 每个文件独立流过所有阶段',
    phases: [
        { title: 'Scan', detail: '扫描文件列表' },
        { title: 'Review', detail: '逐文件审查' },
        { title: 'Verify', detail: '逐文件验证' },
    ],
}

FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {"type": "object"}
        }
    }
}

# 获取文件列表
phase('Scan')
files = await agent(
    "List all changed .py files in the current branch",
    label='list-files'
)

# 解析文件列表（简单实现）
file_list = [
    line.strip()
    for line in files.split('\n')
    if line.strip().endswith('.py')
][:10]  # 限制 10 个文件

if not file_list:
    return {"error": "No Python files found"}

log(f"Processing {len(file_list)} files...")

# Pipeline: 每个文件独立流过 Review -> Verify
results = await pipeline(
    file_list,
    # Stage 1: Review
    lambda file, orig, idx: agent(
        f"Review {file} for bugs and issues",
        schema=FINDING_SCHEMA,
        label=f"review-{idx}",
        phase='Review'
    ),
    # Stage 2: Verify (只对有问题的文件)
    lambda review_result, file, idx: (
        agent(
            f"Verify findings in {file}: {review_result.get('issues', [])}",
            label=f"verify-{idx}",
            phase='Verify'
        )
        if review_result and review_result.get('issues')
        else None
    )
)

# 过滤有效结果
confirmed = [r for r in results if r is not None]

log(f"Pipeline complete: {len(confirmed)} files with confirmed issues")

return {
    "files_reviewed": len(file_list),
    "files_with_issues": len(confirmed),
    "results": confirmed
}
```

---

## 示例 3: Loop-Until-Dry 模式

```python
export const meta = {
    name: 'exhaustive-bug-hunt',
    description: '循环查找直到没有新发现',
    phases: [
        { title: 'Hunt', detail: '循环查找 Bug' },
    ],
}

phase('Hunt')

seen = set()
all_bugs = []
dry_rounds = 0
max_rounds = 5

FINDERS = [
    "Find logic bugs and edge cases",
    "Find security vulnerabilities",
    "Find performance issues",
]

while dry_rounds < 2 and len(all_bugs) < 50:
    log(f"Round {max_rounds - dry_rounds + 1}, found {len(all_bugs)} bugs so far")
    
    # 并行运行所有 finder
    round_results = await parallel([
        lambda prompt=p: agent(prompt, schema=BUGS_SCHEMA, label=f"finder-{i}")
        for i, p in enumerate(FINDERS)
    ])
    
    # 去重
    fresh = []
    for result in round_results:
        if not result or not result.get('findings'):
            continue
        for finding in result['findings']:
            key = f"{finding['file']}:{finding['line']}:{finding['title']}"
            if key not in seen:
                seen.add(key)
                fresh.append(finding)
    
    if not fresh:
        dry_rounds += 1
        log(f"No new findings (dry round {dry_rounds}/2)")
        continue
    
    dry_rounds = 0
    all_bugs.extend(fresh)
    log(f"Found {len(fresh)} new bugs")

log(f"Hunt complete: {len(all_bugs)} unique bugs found")

return {
    "bugs": all_bugs,
    "total": len(all_bugs)
}
```

---

## 示例 4: 使用参数化 Workflow

```python
export const meta = {
    name: 'configurable-review',
    description: '可配置的代码审查 - 支持自定义维度',
    phases: [
        { title: 'Review', detail: '按维度审查' },
    ],
}

# 从 args 获取配置
dimensions = args.get('dimensions', ['bugs', 'security', 'performance'])
file_pattern = args.get('file_pattern', '*.py')

phase('Review')
log(f"Reviewing with dimensions: {dimensions}")

# 并行审查所有维度
results = await parallel([
    lambda dim=d: agent(
        f"Review code for {dim} issues in files matching {file_pattern}",
        label=f"review-{dim}"
    )
    for d in dimensions
])

return {
    "dimensions": dimensions,
    "results": dict(zip(dimensions, results))
}
```

---

## 使用方法

### 从主 Agent 调用：

```python
# 方式 1: 内联脚本
workflow_result = await workflow(
    script="""
    export const meta = {name: 'quick-review', description: '...', phases: []}
    
    phase('Scan')
    result = await agent("Find bugs")
    return {"bugs": result}
    """
)

# 方式 2: 已保存的 Workflow
workflow_result = await workflow(
    name='deep-code-review',
    args={'file_pattern': '*.py'}
)
```

### 保存 Workflow：

将脚本保存到 `~/.minicode/workflows/workflow-name.py`

---

## 最佳实践

1. **使用 schema** - 结构化输出更可靠
2. **限制并发** - 不要一次并行超过 20 个 agent
3. **进度反馈** - 用 `log()` 告知用户进度
4. **错误处理** - parallel/pipeline 会将异常转为 None
5. **去重** - Loop-Until-Dry 必须去重
6. **Phase 组织** - 用 `phase()` 让用户看到清晰的进度
