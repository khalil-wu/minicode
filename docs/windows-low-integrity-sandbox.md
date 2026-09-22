# Windows 沙箱最小可行版：低完整性级别 + Job Object

日期：2026-09-21
范围：`backend/sandbox/win_low_integrity.py`（新）、`backend/sandbox/runner.py`（新增 `low-integrity` 后端）、
`tests/test_windows_low_integrity_sandbox.py`（新，绕过矩阵 17 例）、`tests/test_sandbox.py`（1 处）
上游对照：codex `windows-sandbox-rs/src/{token,process,spawn_prep,env,acl,workspace_acl}.rs`、
`core/src/exec.rs:571`、`sandbox_smoketests.py`

---

## 1. 起点

`SandboxRunner.capability()` 在 Windows 上只认容器（docker/podman），没有时返回 `unavailable`，
`run_command` 按策略 fail closed，模型只能申请 escalation 走无沙箱。桌面端真机日常就是这条路。

## 2. 对照 codex 得出的设计约束

codex 的 Windows 沙箱有两级：`Elevated`（管理员安装：专用沙箱账户 + WFP 网络过滤 + 全盘读 ACL）和
`RestrictedToken`（无需安装）。第二级的机制：

- `token.rs:481` `CreateRestrictedToken(DISABLE_MAX_PRIVILEGE|LUA_TOKEN|WRITE_RESTRICTED)`，
  restricting SIDs = {capability SID, logon SID, Everyone}；
- `spawn_prep.rs:268` 给 allow 路径打 capability SID 的允许 ACE、给 deny 路径打拒绝 ACE，
  `workspace_acl.rs:13` 保护 `.codex`；
- `process.rs:105` Job Object；
- 网络：`env.rs:126` `apply_no_network_to_env`（死代理 127.0.0.1:9、离线 flag、ssh/scp 桩），
  **没有**包过滤，那是 Elevated 级 WFP 的事。

我先照搬 RestrictedToken 做了一版（`.tmp/winsbx/*.py` 探针），绕过矩阵全过，但真机 eval 第一轮
就撞上：`python -m pytest` 在沙箱里失败。根因是 CPython 在 Windows 上 `mkdir(0o700)` /
`tempfile.mkdtemp` 生成的目录 DACL 只有 SYSTEM/Administrators/**OWNER RIGHTS**，而受限 token 的
"restricted 检查"对 OWNER RIGHTS 不匹配，导致子进程连自己建的 `.pytest-tmp` 都读不了
（`accesscheck.py` 5 种 token 形态逐一验证：只有把用户 SID 也放进 restricting 列表才能读，
但那样外部写也全开）。codex 能用是因为 Rust 侧不经过 CPython 的 0o700 路径；
我们的用户天天跑 pytest，这条路走不通。

改用 **Mandatory Integrity Control**：把子进程 token 降到 Low（`SetTokenInformation(TokenIntegrityLevel)`），
给可写根打可继承的 Low 标签（`NO_WRITE_UP`），其它一切对象缺省 Medium → Low 进程写不动。
读不受影响；子进程自己创建的对象继承 Low 标签，OWNER RIGHTS 问题不存在（token 身份没变）。
只读子路径 / 受保护元数据（`.git`、`.minicode` 等）打显式 Medium 标签压过继承的 Low。
标签写入需要 WRITE_OWNER，非提权用户对自己的目录只有 Modify，所以先给自己补一条
WRITE_OWNER|WRITE_DAC ACE 再 `SetNamedSecurityInfo(LABEL_SECURITY_INFORMATION)`（实测 303 个文件的树 0.06s）。
Job Object 与网络 env 改写照搬 codex。

## 3. 实现

| 件 | 位置 |
|---|---|
| 启动器 | `python -m backend.sandbox.win_low_integrity <launch.json>`：复制自身 token 降 Low，`CreateProcessAsUser` 继承 runner 的 stdio 管道，挂 Job（kill-on-close），等子进程退出并转发退出码。runner 的 `taskkill /T` + Job 双保险 |
| runner 接入 | `capability()`：容器不可用且 win32 时，`_low_integrity_unavailable_reason` 为空则返回 `backend="low-integrity"`、`filesystem_isolated=True`、**`network_isolated=False`**；`_wrap_command` 调 `prepare_launch` 打标签、建私有 TEMP（`<state>/data/sandbox-temp/minicode-sbx-*`）、写 spec，返回启动器 argv；`_cleanup_sandbox_setup_state` 删私有 TEMP |
| 不可表示的策略 | 整盘可写、deny-read 路径（标签只管写）、无可写根、非 NTFS 卷 → 报原因、fail closed |
| 网络 | 策略拒网时：死代理 + `PIP_NO_INDEX/NPM_CONFIG_OFFLINE/CARGO_NET_OFFLINE/GIT_*` + ssh/scp/sftp 的 `.cmd` 桩前置到 PATH，并把 `.CMD;.BAT` 挪到 PATHEXT 前面（否则 cmd 仍先找 `ssh.exe`） |

`network_isolated=False` 是刻意的：env 改写挡的是守规矩的客户端，不是 socket；`profiles.py` 会把这个值
原样报给前端，不谎报。真包过滤需要管理员装 WFP（codex Elevated 级），不在"最小可行"范围。

## 4. 绕过矩阵（`tests/test_windows_low_integrity_sandbox.py`，17 例全过）

| 用例 | 结果 |
|---|---|
| 工作区内写 | 允许 |
| 工作区外写 / 删 / 追加 | 拒绝，文件原样 |
| 用户目录写 | 拒绝 |
| 工作区外读 | 允许（与 codex RestrictedToken 级一致） |
| junction 指向外部再写 | 拒绝 |
| 子进程（PowerShell）写外部 | 拒绝（标签随 token 继承） |
| `.git/config` 追加 | 拒绝（显式 Medium 标签） |
| `mkdir(0o700)` + `tempfile.mkdtemp` 后读写 | 允许（受限 token 版在此失败） |
| 沙箱内跑 pytest | 通过 |
| 私有 TEMP 可写、运行后删除 | 是 |
| 宿主 %TEMP% 不被打 Low 标签 | 是（见 §5） |
| 拒网时 env/桩 | `HTTPS_PROXY=127.0.0.1:9`、`ssh -V` 走桩返回非零 |
| 允网时 | env 未改 |
| 超时杀孙进程 | Job 生效，孙进程 pid 消失 |

真机：`minicode_driver` 跑 `inventory` 修 bug 任务（同第 2 项），24 轮、8 次 `run_command`
全部经 `low-integrity` 后端，16/16 通过、CHANGELOG 正确、tests/ 未动；期间 pytest、
`New-Item`、`python -c` 均正常。

## 5. 踩过的坑（勿重犯）

- **宿主 %TEMP% 是缺省可写根**（`policy.py:825` `TMPDIR` special path）。第一版把它也打了 Low，
  等于让沙箱能写所有进程的临时文件，还让 `tests/test_sandbox.py` 里 bubblewrap 语义的用例误过。
  现在 `_wrap_command` 把等于 `tempfile.gettempdir()` 的根排除，子进程只用私有 TEMP；有回归用例。
- `SetNamedSecurityInfo` 写标签需 WRITE_OWNER；ownership 不隐含它。
- 标签探针脚本留在 `.tmp/winsbx/`（`lowil.py` 是最终形态，`probe_restricted.py`/`accesscheck.py` 是被否掉的受限 token 版证据）。

## 6. 未做

- 真包过滤（需管理员 WFP）；deny-read（标签无法表达，需 ACL + 专用账户）；
  `.git` 之外的 `read_only_subpaths` 只在路径已存在时打标签（与 bubblewrap 分支的 synthetic target 逻辑不同步）。
- 标签不会自动清除：工作区目录会一直带 Low 标签（对用户自己的 Medium 进程无影响，Low 只限制低完整性写入者）。
