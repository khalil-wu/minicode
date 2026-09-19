# 危险命令判定：从字符串正则改为解析后的 argv

日期：2026-09-19
范围：`backend/permissions/shell_ast.py`（新）、`backend/permissions/argv_rules.py`（新）、
`backend/permissions/checker.py`、`backend/tools/command_support.py`、`pyproject.toml`

---

## 1. 问题：判定底料是命令字符串，规则得自己当分词器

`backend/permissions/checker.py` 的灾难命令表（改前 `:103-210`）和
`backend/tools/command_support.py` 的副作用分级表（`:390-425`）全部是作用在
**命令字符串**上的正则。`_normalize_for_catastrophic_match`（`checker.py:457`）的
docstring 自己写明这是对"先分词再判定"做法的 approximation：只把引号替换成空格。

于是每条规则都要自己处理 flag 顺序、引号拼接、shell 段切分、变量替换。做不到的地方就是绕过。

### 1.1 实测（改前，`check_catastrophic_command` + `_command_side_effect_kind`）

| 命令 | 硬拦截 | 分级 | 说明 |
|---|---|---|---|
| `rm -rf /` | 拦 | destructive | 基线 |
| `rm -rf /..` `rm -rf /./` `rm -rf //../` | **不拦** | destructive | 根路径模式锚死尾斜杠 |
| `{ rm -rf /; }` `(rm -rf /)` | **不拦** | **workspace** | 花括号/子 shell 让 `rm` 不在段首 |
| `nohup rm -rf / &` | **不拦** | **workspace** | 尾随 `&` 被当成目标的一部分 |
| `echo / \| xargs rm -rf` | **不拦** | **workspace** | 管道尾 |
| `if true; then rm -rf /; fi` / `for f in /; do rm -rf $f; done` | **不拦** | **workspace** | 控制流体 |
| `git "re"set --hard` / `git re''set --hard` | **不拦** | **workspace** | 引号拼接 |
| `git $(echo reset) --hard` / `x=reset; git $x --hard` | **不拦** | **workspace** | 运行期才有的子命令 |
| `timeout 5 rm -rf /` | 拦 | destructive | 但 `git -C sub push -f` 不拦 |

分级是 bypass 模式下"是否强制确认"的依据（`checker.py:1199`）。端到端实测：
bypass 模式下上表所有 **workspace** 行都返回 `PermissionLevel.AUTO`，即免确认直跑；
confirm 模式 + 会话级 `run_command: AUTO` 记忆也一样。

### 1.2 上游做法

codex 把这一层拆成两步，且都在 argv 上：

- `codex-rs/shell-command/src/bash.rs:136` `parse_shell_lc_literal_commands`：用
  tree-sitter-bash 解析脚本，遍历**每一个** `command` 节点，取静态可知的词
  （`parse_literal_command_from_node` `:220`），动态词丢弃。docstring 明说
  "suitable for identifying dangerous literal commands, but must not be used
  to prove that a command is safe"。
- `codex-rs/shell-command/src/command_safety/is_dangerous_command.rs:37`
  `dangerous_command_match`：规则作用在 argv 上（`rm_args_include_force_option`
  `:196` 看 flag 字符而不是正则），`sudo`/`env`/`trap` 按 argv 剥壳（`:123-193`），
  嵌套深度上限 8（`MAX_DANGEROUS_COMMAND_WRAPPER_DEPTH`），超深 fail-closed。

cc 同样用 tree-sitter-bash（`cc/src/utils/bash/ast.ts:1-19`），节点类型走显式白名单，
不认识的结构判 `too-complex` 走人工确认。

## 2. 改动

### 2.1 `backend/permissions/shell_ast.py`：字面命令提取

`parse_literal_commands(script) -> LiteralShell | None`

- tree-sitter-bash 解析；`has_error` 或嵌套超过 8 层返回 `None`，调用方留在原有字符串层。
- 遍历所有 `command` 节点，与所在结构无关（括号、花括号、循环、管道、后台）。
- 每个词按 `word`/`string`/`raw_string`/`concatenation` 求值；含展开字符的词
  （`$`、`*`、`~`、`{}` 除外、反引号、反斜杠转义除外）替换为 `DYNAMIC_WORD` 哨兵，
  规则能看到"这里有个运行期才知道的词"而不信它的拼写。命令名本身动态则整条丢弃。
- `bash/sh/zsh -c`、`sudo`、`env`、`trap` 的载荷递归解析，结果与外层一起返回。
- `compound` 标记只看 `;`/`&&`/`||`/换行链（`list` 节点或顶层多语句），管道不算。
- `lru_cache(512)`：同一条命令在权限门、执行门、分级三处各被查一次。

### 2.2 `backend/permissions/argv_rules.py`：argv 级规则

- `catastrophic_reason(argv)`：`rm` 目标经 `posixpath.normpath` 归一（`/..`→`/`），
  flag 看字符集合，`--` 之后只当操作数；`git` 先跳 `-C`/`-c` 等全局选项再取动词；
  `find` 按谓词遍历，`-exec` 载荷里出现删除命令即命中；`nohup`/`timeout N`/`nice -n`/
  `xargs -I {}`/`time`/`exec` 等透明前缀先剥掉。
- `destructive_reason(argv)`：比 catastrophic 宽一档（`rm -rf build` 属于此）；
  删除类命令带 `DYNAMIC_WORD` 参数、或 `git`/`kubectl`/`terraform` 的**动词位**是动态词
  时，按最坏情况判 destructive。`find` 只在谓词位出现动态词时才这样判——
  `find "$dir" -type f` 和 `find . -name "$p"` 都是 workspace。
- `compound_destructive_reason(argv)`、`is_external(argv)`：对位原来的两张正则表。

### 2.3 接线

- `checker.py:_check_catastrophic_command`：在字符串规则之后、壳层剥离之前调
  `_literal_catastrophic_reason`。字符串层完整保留作为解析失败时的地板。
- `command_support.py:_command_side_effect_kind`：正则判完后再用字面 argv 判
  destructive / external。docstring 里"MiniCode has neither parser on this path"已删。
- `pyproject.toml`：`tree-sitter>=0.25,<0.27`、`tree-sitter-bash>=0.23,<0.26` 进主依赖。
  CI 的 `pip install -e ".[dev]"` 会装。`shell_ast.is_available()` 为 False 时静默退回
  字符串层，因此测试里有一条 `test_parser_is_a_declared_dependency` 让缺依赖**红**而不是跳过。

### 2.4 改后实测

| 命令 | 硬拦截 | 分级 |
|---|---|---|
| `rm -rf /..` `//../` `/./` | 拦 | destructive |
| `{ rm -rf /; }` `(rm -rf /)` `if …; then rm -rf /; fi` | 拦 | destructive |
| `nohup rm -rf / &` `timeout 5 rm -rf /` | 拦 | destructive |
| `git "re"set --hard` `git re''set --hard` `git -C sub push -f` | 拦 | destructive |
| `git $(echo reset) --hard` `x=reset; git $x --hard` `kubectl $(echo delete) …` | 过 | **destructive**（动词位动态） |
| `echo / \| xargs rm -rf` `yes \| rm -r x` `for f in /; do rm -rf $f; done` | 过 | **destructive** |
| `git commit -m "$msg"` `git log --format=$F` `find "$dir" -type f` `bash -c "$payload"` | 过 | workspace |
| `find . -name '*.py' -exec grep -l x {} \;` `git clean -n` | 过 | workspace |

bypass 模式下第 2-6 行现在都是 `CONFIRM`。

单次 `check_catastrophic_command` 未命中缓存约 44 µs，命中缓存 <1 µs。

## 3. 没做的

- 分级仍然**不会**因为解析成功就把命令升到 read-only 快车道。动态词被丢弃，解析结果
  只能证明"有危险"不能证明"安全"，与 codex 同一约束。
- PowerShell 脚本没有走 AST。codex 对 Windows 用独立的 PowerShell AST 进程
  （`command_safety/powershell_parser.rs`）；这里 PowerShell 仍靠原字符串规则。
  是后续项，不在本轮范围。
- `_split_shell_compound`（`checker.py:300`）仍被 `command_support._command_matches_patterns`
  用来匹配沙箱排除列表，未动。

---

## 4. 追加 2026-09-19：PowerShell 脚本走 AST

Windows 上 `run_command` 把脚本交给 PowerShell 执行，模型既会写 `Remove-Item -Recurse C:\`
也会写 `rm -rf $env:USERPROFILE`（PowerShell 别名）。原来 PowerShell 一侧只有字符串正则。
codex 用 `command_safety/powershell_tree_sitter.rs` 把字面 PowerShell 子集降为 argv，再由
`windows_dangerous_commands.rs` 判定；不认识的节点一律 fail-closed。

新增 `backend/permissions/powershell_ast.py`（tree-sitter-powershell）：

- 遍历所有 `command` 节点；`command_parameter` 与 `generic_token` 原样，`string_literal`
  去引号（可展开字符串含 `$`/`` ` `` 视为动态），`@("a","b")` 展开，`-1` 这类一元表达式保留；
  `variable`、`script_block`、`sub_expression`、`invokation_expression` 等替换为 `DYNAMIC_WORD`。
- `powershell -Command/-EncodedCommand` 与 `cmd /c` 的载荷递归（cmd 按 `& && | ||` 切段，
  `%var%`/`!var!` 视为动态）。
- `compound` 只看 `;` 与 `&&`/`||` 链；管道不算，与 POSIX 侧一致。

`argv_rules.py` 加 Windows 规则 `_powershell_reason`：`Remove-Item`/`ri`/别名 + `-Recurse`
对盘根、`C:\Users\<x>`、`$env:USERPROFILE`；`del /s`、`rd /s` 对盘根；`Stop-Process -Name`、
`taskkill /IM`（按进程名杀，与 PID 形式区分）；`Invoke-Expression`。

接线：`checker.literal_command_parses()` 在 win32 或脚本长得像 PowerShell（cmdlet 命名、
`$env:`、`$_`、`-Recurse`）时同时用两种语法读，任一读出危险即判定。
`command_support._command_side_effect_kind` 同源。

实测（`.tmp/ps_matrix.py`）：`Remove-Item -Recurse -Force C:\`、`ri -r -fo C:\`、
`Remove-Item 'C:\Users\bob' -Recurse`、`cmd /c "rd /s /q C:\"`、`powershell -Command "…"`、
`Stop-Process -Name python` 全拦；`Remove-Item -Recurse build` destructive；
`Remove-Item build\out.txt`、`Get-ChildItem -Recurse`、`Stop-Process -Id 1234`、`git log -1`
workspace。依赖 `tree-sitter-powershell>=0.25,<0.27` 进主依赖。
