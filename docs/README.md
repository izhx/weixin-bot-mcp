# 文档

文档整理日期：2026-10-19，Asia/Shanghai。旧版记录按版本和阶段保留，不代表本轮重新验收。

首次部署以 [Docker + Tunnel 部署指南](../DEPLOYMENT.md) 为唯一操作入口，其中包含准备清单、Agent 与用户分工、运行 Key 的权限、私有 `.env` 编辑与可选隐藏输入、扫码、订阅及逐步验收。工具细节再看 [连接 Dot](DOT_SETUP.md)。

| 你想了解什么 | 文档 |
| --- | --- |
| 首次部署、OpenAI Key、扫码和接入验收 | [Docker + Tunnel 部署指南](../DEPLOYMENT.md) |
| 支持哪些消息和媒体 | [能力说明](CAPABILITIES.md) |
| 排查、升级、备份与延迟 | [运维](OPERATIONS.md) |
| 私有部署和数据边界 | [安全边界](SECURITY.md) |
| 验证自己的微信部署 | [验收清单](ACCEPTANCE.md) |
| 参考了哪些成熟设计 | [上游对照](UPSTREAM_AUDIT.md) |
| 贡献代码与运行检查 | [贡献指南](../CONTRIBUTING.md) |
| 准备公开仓库与发行 | [开源发布](RELEASING.md) |

过去的 [源码审查](history/UPSTREAM_AUDIT.md)、[通道修复](history/CHANNEL_REPAIR.md)
和 [部署记录](history/REAL_VALIDATION.md) 保留在 `history/`，描述当时的版本和验证结果。
[URL 接入排查记录](history/URL_MCP_INVESTIGATION_20261019.md) 保留未完成实验的证据，不属于当前部署流程。

[上游 systemd 安装参考](INSTALL.md) 仅供对照原实现；本分支 Docker 部署不执行其中的用户服务安装命令。
