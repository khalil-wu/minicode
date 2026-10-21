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

## 原生 POST 的产品根因修复

第二个候选 `70691945` 的九项 CI 中八项成功，Desktop job 在真实原生 POST 阶段失败；原900×600截图、隐藏等待取消和owner检查通过，其余五项Electron smoke也通过。这不是一轮完整通过。

按 Electron 43.4 的 `guest-window-manager` 合同，`createWindow(options)` 必须采用并返回原 `options.webContents`。MiniCode此前另建第二个WebContents，丢失了原POST/opener；现将原guest交给WebContentsView，并在Chromium创建guest前通过overrideBrowserWindowOptions设置preload、sandbox与隔离偏好。原request继续，未用loadURL重放POST。非GUI定向85/85通过、零skip，真实POST/HTTP/blob仍等待新候选的云端Electron收据。

候选 `4356b948` 的真实Electron已收到原POST、采用原child对象并通过opener检查，随后在getLastWebPreferences的preload字段断言失败；本轮仍不是Desktop通过。字段映射与实际preload consumer正在核查，原sandbox/contextIsolation断言继续保留。

`b9f3688f` 的原POST、child身份及opener再次通过；新增preload功能验证停在native input，20秒deadline真实失败。43.4官方SendInputEvent要求focused窗口，fixture原先为验证离屏compositor而特意仅保留24px可见边缘。现保留全部原capture断言后，才将同一window移到实际workArea、通过原capture管线等真实compositor帧、等待真实focus事件，再发送native input；没有增加sleep、改时限或放松IPC/owner/frame断言。相关非GUI58/58、语法与diff检查通过，新原生结果继续由后续CI判定。

`8a2c1f69` 与诊断候选 `3d53dd86` 的失败同样保留。后者确证原child preload已配置，compositor、focus、DOM trusted ctrl+left click及anchor命中均真实到达；实际console报告 `SVGAnchorElement is not defined`。产品链接判型误用了不存在的构造器，旧unit还虚构了同名全局，掩盖了HTML链接同样会在构造器求值时抛错。现直接使用Chromium标准 `SVGAElement`，同步纠正fixture；不增加typeof兜底，不删除SVG/area/HTML或原native断言。相关5/5与静态检查通过，最终完整native链继续待新CI。

## 真实 V2 会话回放与工作记录修复

脱敏fixture来自 `runtime-live-short-05` 的真实成功wire，保留24条client、1175条server及断线/恢复等audit事实；只去掉255条独立OS采样，替换57处实际provider base URL，不导出用户配置或凭据。fixture明确标记232.406秒short及原wire/result/backend输入哈希，不用作小时通过依据。

回放发现真实projection缺陷：同child epoch2的done已推送工作记录，后续回切会话的durable UI快照仅含子agent元数据，整对象hydrate丢失了工作记录。现仅在原conversation、child、agentPath及mailboxEpoch相同时保留已收到的transcript缓存；当前快照继续负责其余状态。跨会话、path或epoch不借用旧记录。

新V2真实replay1/1通过，原20261020 case本次未运行、筛选skip1；sessionEvents、SubagentsTab和processDisclosure合计114/114。共115个不同用例通过，TypeScript通过。严格验证child epoch[1,2]、工作记录非空且工具全部settled、新B实际往返final、队列、离线完成快照、草稿、精确最终cursor及updater无遗留后台任务。
