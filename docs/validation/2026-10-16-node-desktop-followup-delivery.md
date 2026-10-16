# Node、桌面投影与安装候选后续交付

本页记录本轮实际交付范围，不表示整款 MiniCode 已与 Codex 等价。后端参照 checkout `822e58cc3d666166c7446c5b1ea2e52f5d09594c`；桌面视觉及交互参照实际截图和提取资源。官方账号按用户要求排除。字体、字号和字重保持原样。

## 实际变化

- SDK 的 Node 状态归长期 SDKSession，WS 归实际 conversation thread，子会话保留独立内核。公开审批回调透传完整，两次实际请求均在批准后执行；关闭原 Node ACK 先于环境释放。
- 原 host 的合法 None 被明确保留；SDK fork、子 agent、Guardian、网络审批沿原 host 传递。动态 builtin 配置及缓存由当前执行快照重新准入，同名用户 MCP 和普通 lazy catalog 保留。
- Node Inspector 控制移到 supervisor；Worker 保留原 VM、ALS、顶层绑定、npm 和图片。直接 CPU、初始化重置、超时/取消交错及异常退出均结算原请求，重置等待真实 Worker exit。严格指令、hashbang、this、重声明及晚输出归属均覆盖。
- MCP js 过程短行使用实际 title。外观增加真实三色 picker，浅/深色独立保存，页面和 Monaco 同步，原编辑模型、未保存内容及字体保留。设置、provider、设备/Problems 菜单及 leading-dash 预览令牌修复另见对应报告。

公开 Source 没有 Node 执行器实现，本地 companion 是对可观察工具契约的实现，不宣称复刻未公开内部服务。Computer Use 尚未随包提供。

## 分范围验证

各范围有重叠，不累计为产品通过数。原始失败、跳过及中止均保留。

| 范围 | 实际结果与证据 |
| --- | --- |
| SDK/MCP/Notes/observer | 89 个不同 case 通过；`output/ci-ee80a807-followup-20261016/` |
| 原 host 与子链 | 主 56 节点按最新替换为 55 passed / 1 installed Source stub skip；子链 32 passed / 2 native proxy skip；`HOST-ADMISSION.md` |
| 最终内核消费者 | 43 passed / 1 native skip，Node 22/Electron 新八项各 8/8；三运行时各 100 双 reset + 20 direct CPU；`output/node-kernel-quiescence-20261016/` |
| 代码源 LF 归一 | 实际 Python read_text 执行文本不变，归一后真实 MCP 八项 8/8；`root-canonical-source.json`、`root-canonical.xml` |
| 外观、聊天 | 外观唯一 195/195；聊天相关四文件 80/80；TSC/build/两项预算通过；[外观报告](2026-10-16-appearance-colors.md) |
| 生产 Electron Node 投影 | 真实两轮 Node 41→42、同 PID、两份 PNG 经 CodeMode/WS/模型转发；完整 final 后先自动折叠，再展开原证据；草稿保留、pageerrors=[]；下节详述 |

## 当前构建的真实桌面

前端 entry `assets/index-CCh13oC-.js`，index SHA256 `17dff27b79573909e5cedaf6d1a27dff9e5465c65fed84c31f23dbb6a69ba129`。主 CSS `index-D5-lmIEl.css` 沿原字节，实际字体仍为原 Anthropic/CJK 字体栈、13px、400。

`output/playwright/latest-ui-followup-20261016/validation.json` 保存真实生产 Electron 设置/配色的十项观测、六图。`output/playwright/node-production-ui-20261016/report.md` 保存 Node 实际 WS/CodeMode/MCP 链的原始结果；run02 两轮实际 PID 21056、Node 24.18.1，完成前未点击原始详情，final 后自动折叠。

Root 后续 run03-settled 等图片 decode 与有限动画真实完成，未修改生产。两轮图片的 natural size 为 192×96，渲染为 754×360，均完整位于各自 gallery 和可见 viewport 内；两张稳定展开图和五张其他状态图在该目录，`validation.json` 保存几何和 SHA。run02 的瞬时裁切图仍保留，不冒充稳定整图证据。两轮均无真实 CodeMode yield，不能算 tool_wait 实机通过。

Node 投影 fixture 从一开始使用公开 bypass，因此真实策略 Never、Node 审批数 0，不假造 UI 审批。独立普通 MCP confirm/on-request fixture 另通过三项检查：真实“允许此次对话”、审批替换 composer、草稿恢复及下一轮复用。SDK 安装验收的两次真实审批单独记录。所有 QA 使用私有 profile/workspace，仅模型 HTTP 受控，未操作普通用户窗口或根 data。

## r13 后端安装候选

冻结 backend `0c10889e880b97a32bd962e4d74102843c4524a9`。新 wheel 为 16,093,169 bytes，SHA256 `9f19f471050767b12593dd9d17931bde42b5744328f4f8ba039971b83dec6b6f`。bundled Python 3.11.9 的全新 `-I` 安装实际 **41/41**，包括原四十项和独立 startup logger 健康门，真实 ERROR 列表为空。

1,271 个冻结输入、1,250 个 backend wheel 成员在 current/frozen/wheel/installed 物理字节一致。Git 内容全部匹配冻结提交：1,104 项 raw 相同，167 项仅历史 checkout CRLF/Git LF 差异，其他内容差异 0；每文件双 hash 完整保留。最终 MJS raw/canonical SHA 均为 `7f1177fa2c5d8bdde6361362087fb55e3d3a7407bf116c81d5d03bf8c39ce30b`。

`output/source-executor-remote-final-wheel-r13-20261016/REPORT.md` 和 `delivery-receipt.json` 是安装范围凭据。clean_acceptance=true 仅适用于该后端候选；candidate_only=true、whole_product_equivalence=false。r12 的四十断言和三次实际启动错误继续保留，仍 stale/hold；没有借旧包结果。原生 ACL、unelevated deny-read、外机及 Linux 安装边界未被改写为通过。

## CI 与多窗口退出复验

`25e98f7a` 的 [CI 38910536542](https://github.com/khalil-wu/minicode/actions/runs/38910536542) 八项成功。包含新内核的 `0c10889e` [CI 38917884820](https://github.com/khalil-wu/minicode/actions/runs/38917884820) 则保留真实 Desktop multiwindow 退出失败；不能用前一轮绿灯覆盖。

该用例在最后窗口关闭时提前进入 Electron 默认退出，且末尾强制 app.exit；成功短行位于异步清理之前。fixture 现接管 window-all-closed，等待原 PTY、bridge、HTTP server 的 ACK 后正常 app.quit。父测试等待 close，严格核对 `{code:0,signal:null}` 和三个清理/quit 标记，catch 仍失败。仅修改测试，没有固定等待、重试或生产错误隐藏。本机原用例 1/1 通过，完整诊断及保留的 Windows AttachConsole 错误见 `output/desktop-exit-20261016/report.md`。

退出修正后的完整 CI 使用独立提交及原始运行记录，见[本分支 CI](https://github.com/khalil-wu/minicode/actions/workflows/ci.yml?query=branch%3Acodex%2Frelease-readiness-20260927)。最终结论按相应冻结 SHA 的原 API、logs、XML 读取；本页不将 Windows 窄验当作 Linux Napi 错误已消失的证据。

仍未证明或未实现的范围统一在[当前差距](2026-10-16-current-desktop-gaps.md)：系统 Computer Use、完整代码主题/分享、私有 MCP App relay、外机 SSH/WSL、WFP/原生权限和最新全界面全部交互状态，不以本批通过数推算完成率。
