# 本轮候选代码与 CI 验证

首个候选 `793e3b3d` 已推送，完整 CI 收据为 <https://github.com/khalil-wu/minicode/actions/runs/39569701407>。其失败未被当作通过：

| 范围 | 首个候选实际结果 | 修复后的定向证据 |
|---|---|---|
| Frontend | 5171 passed / 3 failed | `ci-frontend-repair.json`：6模块95/95；类型检查通过 |
| Root Python | 1786 passed / 2 failed / 52 skipped | `ci-root-contract-repair.xml`：3/3，包括真实 AWS factory/签名回归 |
| Backend Python | 7633 passed / 3 failed / 162 skipped | `ci-backend-fixtures-repaired.xml`：4/4 |
| Desktop unit | 370 passed / 0 failed / 9 skipped | 首个候选实际CI，不能把平台skip计为通过 |
| Desktop Electron smoke | browser-capture 在25秒超时，未回收的原子进程/stdio导致整个job最终取消 | `ci-desktop-capture-parent-repair.log`：非GUI66/66，含原失败保留、日志、真实close及超时回收；真实native POST/HTTP/blob仍需后续CI |
| Native execution/network、Linux wheel冷安装、Windows harness、Electron frontend integration | 对应5个job通过 | 首个候选实际CI；不扩大为全部桌面/设备验收 |

## 修复范围

前端修正缺少必填 `command/level/message` 的旧命令结果 fixture，生产协议检查没有放松。分组 mutation 与新建/移动/编辑 callback 按真实依赖稳定，父组件无关更新不能再让全部会话行重渲染；原性能断言保留，并补最新 section membership 与原草稿/会话归属用例。

普通 OpenAI HTTP client 不再收到无关的 AWS `auth=None` 参数；只有实际 AWS 配置会注入签名 consumer。Windows sidecar lock 同步新增的 `httpx-sse==0.4.3` 与 `botocore==1.43.115`。当前 bundled Python 只增装原先缺少的 SDK/依赖，未删除或重建正在使用的 runtime；`bundled-bedrock-import.log` 验证实际 Python 3.11 SDK、SSE 与八模型目录导入，没有真实 AWS 请求。

Backend 三个旧 fixture 分别补齐真实 repository 的 sections consumer、以真实 AWS mapping 声明能力，并修正 V2 actor 协议与接收模型工具准入不能混为一谈的断言。V1模型保留V2 actor身份不会得到V2工具，也不能降回legacy task；生产 gate保持原样。

浏览器 smoke 保留 native POST、opener、原下载、blob 与 owner 的全部断言。父 fixture 注册并等待包括 stdio 的原 `close`；原20秒deadline及25秒case时限不变，失败后按已有smoke方式 TERM→等待→KILL→真实close，保留原错误和失败profile。阶段日志实时输出，HTTP成功记录位于blob之前。没有修改浏览器manager来猜测native停点，也没有在本机运行Electron/GUI/CDP。

## 长任务验收器

实际试运行发现的 URI、流式参数预览、公开结果无内部command ID，以及child actor ID与真实query turn的区别均已按现有producer修正。审计器保留原PID/create-time、独立OS退出/时长、原durable owner、实际启动/完成通知、真实monitor结果、同child epoch和SQL/工作记录关联；不解析summary补证，不新增public内部状态，不放松重启/换handle/跨owner拒绝。另修正CLI选择state root之前导入backend配置的初始化顺序，原用户配置未改变。

进程、V2 board和现有client定向146个用例通过；最新board修正75/75，另CLI import单例通过。它们不是一小时收据。真实短任务校准和正式3600秒验收单列于 `2026-10-21-real-hour-acceptance.md`。最终GitHub HEAD及全部九个job结果待最终收据补充。
