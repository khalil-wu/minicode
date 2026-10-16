# 设置、面板选择菜单与静态预览后续修复

本页只记录本批范围，不声明整款产品与 Codex 等价。字体、字号和字重保持现状。界面依据为已有真实 Codex 截图及已提取桌面资源；当前同构建验证来自私有 profile 和自有工作区的真实 Electron，模型 HTTP 为自有受控端点，未操作普通用户窗口。

## 修复内容

- 设置页面使用标题和原有控件层级，删除额外增加的页面说明。说明仍保留在搜索元数据，不影响按现有关键词检索设置。对应常规、外观、Git 原图为 `output/playwright/ui-reference-20261004-v2/desktop/07`、`08`、`15` 开头的 PNG。
- 模型设置此前只读取 `provider_history`：实际有效配置已经能运行模型，但没有历史项时仍显示“尚未配置提供商”。现在同时呈现真实有效配置；同一端点已有保存卡片时保持一项，编辑继续使用原端点配置与凭据入口，没有添加虚构预设。
- 浏览器设备、诊断筛选以及问题面板的级别、来源筛选改用已有 `SelectMenu`。保留同一状态、设备实际尺寸、诊断切换重置、问题来源及精确诊断导航。选择文字不截断，选中标记、键盘与关闭行为复用已有桌面菜单。
- 静态预览命令将令牌参数改为 `--token=<value>`。`secrets.token_urlsafe` 可能以 `-` 开头，原来的 `--token <value>` 会被 argparse 解释成缺少参数，服务随即退出。没有添加重试、固定等待或放宽 URL/文件检查。

## 证据与结果

`output/playwright/desktop-followup-20261016/ui-validation.json` 保存入口、SHA 及逐批测试记录。最新 entry 为 `assets/index-BRD0SxXm.js`，index SHA256 为 `b5a91925a1b8ab4b9e1ec3bc43ea0f53d62cf019552a58380b2571e6b5292d88`。全局 CSS `assets/index-D5-lmIEl.css` 与此前构建逐字节相同；浏览器设备控件的实际字体族、13px 和 400 字重另在新旧控件之间直接核对。

前端必要范围最新唯一 **170 passed**，不是一次全量产品重跑。首批 85 项中有 1 条旧副标题期望失败；修正该旧期望后重跑原节点通过。面板及共享选择器另批 96/96，与前批重叠的 11 个选择器节点只计一次。初败、窄复验和各批 JUnit 均保留。TSC/Vite、bundle budget 和 UI debt budget 均通过，没有提高限制。

真实 Electron 验证包括：有效配置无历史项时正确出现并可打开原编辑页、同 profile 重载后 `UNSENT_FOLLOWUP_DRAFT` 保留、实际打开设备菜单并选手机后出现 390×844、设备字体前后一致。最后两项截图为 `15-browser-device-menu.png`、`16-browser-phone-dimensions.png`。最初若干设置截图是异步加载中的状态，不作为最终稳定状态通过；检查脚本的过期按钮等待、有限动画取消及一次 shell 引号错误保留于原记录，不记为产品功能通过。

静态预览必要测试 **16 passed / 2 skipped**。两项真实沙箱测试在本 Windows 宿主缺少适用隔离权限时明确跳过。另以 bundled Python 实际复现旧参数 exit 2，修复参数则真实提供 HTML、CSS，对 `.env` 返回 404，并关闭原 PID，证据为 `preview-token-cli.json`；该独立 CLI 验证不冒称 OS 沙箱通过。

GitHub run `38899337955` 是 Linux 检查点 `9f34e349`，尚不包含本页修复。它的 root suite 原始结果为 1767 passed / 51 skipped / 1 failed，失败令牌为 `-ciB7…`，原失败证据没有改写。修复后的完整 CI 由后续提交单独记录。

## 明确保留的差距

本次外观截图证实当前 MiniCode 仍缺少参考图中的代码主题和主题配色控件，不能把已有模式选择或高级折叠算成这些功能。完整同构建、所有区域和所有 hover/focus/disabled/过渡状态仍需继续核对。本页没有实现或虚构这些控件。
