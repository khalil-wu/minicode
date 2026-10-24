# Runtime、桌面布局与交互审计验收记录

代码分支：`codex/source-desktop-parity-20261024`；[PR #1](https://github.com/khalil-wu/minicode/pull/1)。功能和测试修复的已验证基线为 `c23580eb95ca32ef1651823444b3dc2c32a04cf0`。

## 已通过的基线

- [Push CI](https://github.com/khalil-wu/minicode/actions/runs/40091795084) 与 [PR CI](https://github.com/khalil-wu/minicode/actions/runs/40091797278) 均完成，分别10/10 jobs成功。跳过用例保留原统计，不计为通过。
- 真实 provider 任务249.563秒，16条 canonical 工具记录，两名 child 实际重叠59.182秒，真实 mailbox 发送与确认，独立6/6结果校验。修复后可见窗口恢复验证最终答案、两份 child 工作记录和另一聊天草稿；没有重新请求付费模型。[详情](C:/Desktop/MiniCode/docs/validation/2026-10-24-full-task-projection.md)
- 完整受控生产 renderer 审计：34检查、110捕获、0 renderer errors；包括重连/恢复/草稿、17可见设置页、三级主题菜单状态、实际配置与导入读写、真实导入失败、菜单子层和自动化表单。DPR覆盖1/1.25/1.5。
- 110张原图均已逐张静态审阅：53张主设置/对比度，57张其余状态。发现的个性化链接颜色差异已修；未确认新的明显且有当前Source依据的视觉缺陷。[其余57张审阅](C:/Desktop/MiniCode/docs/validation/2026-10-24-ci-ui-2af69cd9-static-review.md)
- 当前逻辑像素量尺：正文与composer同轴736，composer总高98/底栏28/控件28，菜单顶栏36/桌面主strip52，设置卡片728/开关32×20；保留特色字体、显式字号和缩放偏好、原侧栏宽度、草稿及未保存buffer。[布局记录](C:/Desktop/MiniCode/docs/validation/2026-10-24-layout-parity.md)
- Native unload oracle 验证真实两次reload、关闭前提交、正常退出和冷进程恢复2MiB附件。没有通过接回已移除的整窗Ctrl+R快捷键、增加timeout或强制退出来让用例通过。

## 不能扩写为已完成的范围

- 上述110张是静态捕获，有限动画被快进；不证明自然动画、所有hover/drag/中断状态、所有原生窗口或每一个按钮的全部后续分支。
- Source注册页、当前可见页与尚未开启的gate分开；24个Settings tab和所有surface的入口、handler、transport、response owner、Source依据及未验项见[完整交互覆盖表](C:/Desktop/MiniCode/docs/validation/2026-10-24-interaction-coverage.md)。没有以截图数或测试数计算全产品完成率。
- Windows首次Node companion启动的timeout没有被修复或改算通过。后续成功迟缓记录把主要等待定位到outer launcher ready后、Node OS创建前约18–20秒；同profile暖路径约1.9秒。去掉继承的Crashpad管道只消除了拒绝日志，没有消除主要迟滞。[调查](C:/Desktop/MiniCode/docs/validation/2026-10-24-node-repl-startup-investigation.md)
- 用户参考窗口的computer-use被physical Escape停止后未继续接管。后续独立MiniCode测试进程、静态PNG审阅与既有Source现场观察分别记账；未观察的桌面分支仍待验。

本记录及后续调查补充只更改文档，不更改已验证基线的生产代码。最终GitHub head仍以PR最新Actions检查为准；PR保持draft，不声明全功能、全视觉或全动画一比一完成。
