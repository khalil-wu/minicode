# Amazon Bedrock 本地配置界面链路

本批在现有提供商设置内接入 Source 的 Amazon Bedrock 配置流程。行为依据为公开 Source `822e58cc3d666166c7446c5b1ea2e52f5d09594c` 的 Bedrock 登录、凭据发现与 `account_processor/bedrock_gov_cloud.rs`，以及本机当前 Desktop `26.1016.6573.0` 的对应页面消费代码。后者只构成本报告的 Bedrock 参考更新，不代表已重新验收该版本的全部桌面功能。

参考文件从安装目录 `C:/Program Files/WindowsApps/OpenAI.Codex_26.1016.6573.0_x64__2p2nqsd0c76g0/app/resources/app.asar` 只读提取，存于 `output/full-parity-20261021/bedrock-reference-current/`。页面为 `login-route-fa0e17e1b4dc.js` 的 `gn`，入口与装载参考 `login-page-e18e325766e5.js`、`onboarding-login-content-80b686315c23.js`。原版本 `26.1013.4894.0` 的 `DPs`、`OPs`、`SPs/kPs` 作为已记录交叉证据。

## 已接入的完整链路

既有提供商类型选择内显示“使用 Amazon Bedrock”。配置页按需装载，使用现有打包的 Bedrock 品牌图标；没有增加全局入口、区域下拉框或 endpoint 切换按钮。已识别的 AWS 配置不会再显示通用 URL、API key 和协议格式输入，原模型设置与保存的 endpoint 保留。

首先调用真实凭据发现接口。检测到的 profile 使用带区域说明的选择菜单，继续时提交所选 profile 自己的区域；所选 profile 没有区域则打开空白区域表单，不借用环境变量区域。检测到环境凭据时才显示对应入口，环境凭据留在宿主侧。没有 profile 时仍可手动输入；只有成功的空发现结果显示“未找到 AWS 凭据”，发现失败保留真实报错。离开页面取消未完成的发现。

四种表单分别映射到后端 `profile`、`environment`、`amazonBedrockAccessKeys` 和 `amazonBedrock`。区域是文本输入；profile 和区域去除首尾空白；secret、session token 和 API key 遮罩，空 session token 提交 null。选择其他登录方式保留该页已输入凭据。返回后恢复原提供商草稿及入口焦点。

提交前捕获发起会话与工作区。凭据保存后通过 `llm.config.set` 应用到原会话；用户已切换到另一会话时，不覆盖其模型投影。已经保存但应用失败会明确报告失败并保留草稿，不显示为全部成功。配置成功后调用真实 GovCloud 检查：按检查结果显示已知警告，读取失败进入 Source 的未验证指引；再次检查仅重读检查接口，不重复发送凭据。指引链接使用 Source 的 GovCloud 配置文档，知悉后才结束该页面。

## 验证证据

前端 **48 个不同的当前用例**有最终通过证据：原批次的 33 项既有 Provider、SelectMenu 和 ModelBrand 回归，加最终修复的 15 项 Bedrock 链路用例。该数字合并最后一次执行结果，不宣称有一次新的 48 项运行。

- `output/full-parity-20261021/bedrock-frontend-chain.json`：首批 48 项，37 通过、11 失败；新页面区域提示被包括在 label 的可访问名称内，按真实无障碍结构修正。
- `bedrock-frontend-repair.json`：新页面 15 项，12 通过、3 失败；GovCloud 文案键尚未合入，保留真实运行错误，未添加翻译兜底。
- `bedrock-frontend-final.json`：文案合入后 15/15 通过，包含 ProviderTab 按需装载的最终集成。覆盖 profile 区域、环境发现、取消、真实错误、秘密遮罩、草稿恢复、四种映射、原会话所有权、保存后应用失败、GovCloud 确认与重查，以及打包图标。

本 agent 未重复运行全局 TSC、构建或 CI，由主 agent 在各组冻结后统一验证。未进行本机 GUI、computeruse、Playwright、CDP、像素级验收、真实 AWS 凭据连接或 GovCloud 网络环境验收；受控接口回执不能计作这些外部环境通过。
