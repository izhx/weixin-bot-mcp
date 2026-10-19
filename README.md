# weixin-bot-mcp

文档更新日期：2026-10-19，Asia/Shanghai。

基于 [cd233ljx/wechat-dot-bridge](https://github.com/cd233ljx/wechat-dot-bridge)，增加本人主动通知、Docker 和宿主磁盘持久化。服务器只负责微信收发，消息经 MCP Events 触发你已有的 Dot。

**本仓库请按 [Docker + Tunnel 部署指南](DEPLOYMENT.md) 安装和运维。**

本分支 MCP 服务 / Docker 镜像版本为 **0.2.4**，共十三个工具，部署路线为 **Docker + Secure MCP Tunnel**。既有验证记录见 [部署验收](docs/DEPLOYMENT_VALIDATION.md)；每个新部署仍需实际验证微信收发。

---

在微信里和你已有的 OpenAI dot 聊天。

你发来文字、图片或文件，桥接把来信交给 dot，再把它的回复送回同一个微信对话。模型能力仍由原 dot 提供，桥接本身只负责消息收发。

## 能做什么

- 文字聊天，同一条来信可以先回进度，再回结果；长回复自动分段。
- 收取和回传图片、文件、视频，读取文本附件，处理引用内容。
- 接收微信语音及平台提供的转写；其他音频转换需要可选编解码器。
- 保存待处理消息和发送状态，提供输入提示、备份与故障恢复。

当前分支为实验性版本，面向 Linux / Docker 和本人单账号私聊。具体能力和微信端确认情况见 [能力说明](docs/CAPABILITIES.md)。

## 开始使用

从 [Docker + Tunnel 部署指南](DEPLOYMENT.md) 开始，按准备清单逐步完成主机检查、OpenAI 组织与工作区关联、Tunnel ID 和专用运行 Key、微信扫码、私人插件、精确回调白名单与唯一订阅，最后在手机上确认实际收发。指南也包含可以交给 Agent 的部署任务说明与每步通过条件。

接入需要已有 Dot、私人插件权限，以及本人可用的 [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 和 [MCP Events](https://developers.openai.com/plugins/build/mcp-events) 能力。运行 Key 用于 Tunnel 控制面认证，可由用户自行编辑私有 `.env`，运行代理也集中在其中配置；终端隐藏输入方式仍兼容。桥接不直接调用模型。Docker 镜像安装运行依赖，源码开发环境另见贡献指南。

插件应只供自己使用。消息和附件保存在本机私有目录；dot 读取后的内容仍会进入你的 OpenAI 会话。发送结果不确定时会保留 `uncertain`，由你核对后处理。

## 文档

- [Docker + Tunnel 部署](DEPLOYMENT.md) · [连接 Dot 与工具清单](docs/DOT_SETUP.md)
- [排查、升级和备份](docs/OPERATIONS.md) · [微信端验收](docs/ACCEPTANCE.md)
- [贡献指南](CONTRIBUTING.md) · [变更记录](CHANGELOG.md) · [安全报告](SECURITY.md)

## 实现与致谢

Python、官方 MCP SDK、AnyIO、HTTPCore、Standard Webhooks 和 SQLite 负责协议与运行机制，依赖版本保存在 `uv.lock`。

微信协议参考 [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin)，微信通道和会话生命周期参考 [cc-connect](https://github.com/chenhg5/cc-connect) 的固定版本；参考版本、调用链和适配差异见 [上游审查](docs/UPSTREAM_AUDIT.md)。

原创代码采用 [MIT](LICENSE)，第三方来源见 [NOTICE](NOTICE.txt)。本项目与 OpenAI、腾讯无隶属或背书关系。
