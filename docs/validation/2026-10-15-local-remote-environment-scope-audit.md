# 本地可配置远程执行环境：当前边界只读核对

2026-10-16 状态更新：本页保留最初只读审计时的调用链和待办事实，不再代表当前未完成清单。后续已实现 registry/connection、receiving-thread selection、目标命令/文件/checkpoint、MCP/Skill、编辑器/API 和 SSH/WSL desktop host，交付索引见 [后续实现与验证](2026-10-15-source-internal-evidence-delivery.md)。实际外机 SSH/WSL、Windows WFP provision、独立 executor LSP/PTY 和 Linux OS managed routing 的边界继续按新报告保留；不能从本页的最初“未实现”判断当前代码，也不能将后续本地验证扩大为外机验收。

本文件为后续范围审计，未实现或验证远程 executor，也没有增加桌面控件。参考 `.tmp/codex-src`，commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。不将此前“远程环境/高级审阅”这一笼统待办当作已完成证据。

Source 的可配置执行环境不全依赖官方账号。`codex-rs/exec-server/src/environment_toml.rs` 从 `environments.toml` 读取真实 `default/include_local/[[environments]]`，每项可以使用 WebSocket `url`（可选 bearer token）或 `program/args/env/cwd` 的 stdio executor，不能将两者替换成只在界面上切换的环境名称。普通本地环境仍是独立的内置项。

`app-server-protocol/src/protocol/v2/environment.rs` 与 `app-server/src/request_processors/environment_processor.rs` 定义真正的 `environment/add`、`environment/info` 和 `environment/status` 消费者。`status` 查询已有连接观察到的 ready/pending/disconnected/unknown，不启动或恢复环境；`info` 才调用 `force_info`。不能让状态查询触发隐式连接，也不能仅根据配置存在就显示 ready。

`protocol/src/environment.rs` 明确区分 caller request 和接收 thread 构造的 runtime selection。实际选择包含 environment ID、URI cwd、workspace roots 和该 attachment 的 config/permission 事实。Pending/Failed authority 不能当作 Full Access。子任务需要由接收者构造自己的 selection，而不是把父 thread 的运行状态对象当成新输入。

`core/src/tools/handlers/wait_for_environment.rs` 的工具只服务真实 starting 环境，既不是所有回合默认出现的工具，也不是与远程环境无关的通用 loading UI。远程环境的 required skills 由实际环境声明并在模型执行前检查；这要求远程 filesystem、命令、Skill/MCP、权限及生命周期完整接到同一个 executor。

当前 MiniCode 的 `backend/terminal/stdin_authority.py`、`backend/permissions/context.py`、command/permission consumer 已用 environment ID 保留原操作授权归属。这些字段不等同于环境注册、连接、executor filesystem 或远程命令能力。当前 terminal manager 仍执行本地命令；没有找到 Source 的 `environments.toml`、remote executor registration/info/status 和 thread selection 端到端生产链。因此本地可配置远程 executor 仍是实际未完成范围，不能因用户排除官方账号而一起划掉。

下一次实现前应继续核实际桌面入口和当前可用 gate，再沿上述 registry → selected thread authority → executor/filesystem/tools → original task lifecycle 接完整行为。不能因 Rust 暴露某个 API 就自行添加截图里没有的常规控件。官方账号/hosted 远程服务继续排除；本文件没有运行连接、工具或实机用例，不计为通过。
