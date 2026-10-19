# 微信 MCP 的 URL 接入尝试与暂停记录

记录日期：2026 年 10 月 19 日，时区 Asia/Shanghai。文中公网地址统一使用文档示例 IP `203.0.113.10`，不对应真实部署；真实服务器地址、SSH 别名及私人源码和部署路径已省略。

URL MCP 方案已按本人要求暂停，继续使用已经完成文字收发和主动通知验收的 Secure MCP Tunnel。公网 HTTPS 和服务端 OAuth 接口已通过独立探测，但平台自动发现没有成功，不能视为 Dot 已通过 URL 接入。

暂停 URL 后优先处理原 Tunnel 部署的图片附件读取错误 `media_network_failed`；该故障随后已在 0.2.3 修复并通过真实 Tunnel 图片读取验证，本人也已确认微信新发图片能被 Dot 识别，详情见 [图片附件修复验收](../DEPLOYMENT_VALIDATION.md)。本记录保留 URL 试验的事实、误判修正和后续恢复条件，不包含密码、运行密钥、完整事件回调地址、签名密钥或微信正文。

## 目标和运行基线

微信收发程序以 Docker 运行在部署服务器，配置、微信绑定、订阅、队列及认证状态持久保存在项目的宿主 `data/` 目录。

暂停 URL 试验时，实际运行镜像为 `weixin-bot-mcp:0.2.2`，桥接容器健康；微信账号 `ready`、原 Tunnel ready、HTTP ready，存在一个有效订阅。URL 与 Tunnel 共用同一桥接进程和微信轮询器，没有迁移订阅，也没有切换到 direct 模式。

本人曾在手机上确认文字来信触发 Dot 回复，以及 Dot 主动通知到微信。图片、文件、视频和语音不因文字验收通过而自动视为通过。

最初希望用 URL 减少回复等待时间，但尚未测出原 Tunnel、事件回调、Dot 处理和微信发送各自占用的时间，不能断言慢的根因是 Tunnel。

## 已部署的 HTTPS 和 OAuth

实验地址在本文中以 `https://203.0.113.10/mcp` 表示。独立 Caddy 网关以 Docker 运行，使用固定镜像的 Caddy 2.11.4 和 Let's Encrypt `shortlived` profile 的 IP 证书，证书 SAN 包含实际部署的 IPv4 地址。外部探测正常验证证书，没有使用 `-k`。

服务端使用官方 MCP SDK 提供 Streamable HTTP、动态客户端注册和 PKCE。本人已在 SSH 终端隐藏设置授权密码，服务器保存 scrypt 哈希；授权状态和 TLS 证书保存在磁盘。

| 用途 | 脱敏后的示例地址或值 |
| --- | --- |
| MCP URL 和 resource | `https://203.0.113.10/mcp` |
| 受保护资源元数据 | `https://203.0.113.10/.well-known/oauth-protected-resource/mcp` |
| 授权服务器元数据 | `https://203.0.113.10/.well-known/oauth-authorization-server` |
| issuer | `https://203.0.113.10/`，保留末尾斜杠 |
| 授权端点 | `https://203.0.113.10/authorize` |
| Token 端点 | `https://203.0.113.10/token` |
| 动态注册地址 | `https://203.0.113.10/register` |
| 默认 scope | `weixin` |
| 客户端 Token 认证 | 表单选择 `client_secret_post` |
| PKCE | 元数据实际包含 `code_challenge_methods_supported: ["S256"]` |

未授权的 `/mcp` 请求返回 401，`WWW-Authenticate` 指向资源元数据。资源元数据和授权服务器元数据返回 200，动态注册地址明确存在；公网健康路径返回 404。以上证明服务端接口可以被独立客户端访问，不证明平台已经读取或接受这些接口。

## 平台表单和错误

平台认证选 OAuth，客户端注册选 DCR；Client ID 和 Client Secret 留空，由动态注册生成。DCR 曾显示灰色，手动填写 `/register` 后可以选择；OIDC 保持关闭。

创建连接时曾收到 HTTP 400：

```json
{
  "detail": {
    "type": "apps_sdk_error",
    "error_code": "app_validation",
    "message": "OAuth authorization server metadata must advertise PKCE support with code_challenge_methods_supported containing S256."
  }
}
```

服务端当时实际元数据已经声明 S256。手动填写表单并不能证明平台成功读取了服务端元数据。

随后本人明确确认：刚填完 MCP 服务器 URL，自动发现就返回以下错误（地址已替换为示例 IP）；点击“重新获取 OAuth 设置”仍失败。这发生在创建连接和密码登录之前。

```json
{"detail":"MCP server https://203.0.113.10/mcp does not implement OAuth"}
```

界面同时显示“未能自动获取 OAuth 设置。请检查服务器 URL 后重试，或手动输入设置”。排查期间 OAuth 数据库没有生成客户端或授权记录。

## 网络证据和结论边界

### Dot 云端 curl 的证书错误

本人让 Dot 从实际云端环境执行 curl，未使用本机 Mac。`/probe`、资源元数据和 `/mcp` 都返回 curl 60、HTTP 000：证书 SAN 不包含目标服务器 IP。

该云端环境设置了 HTTPS 和 ALL_PROXY 代理变量，实际收到的证书由 OpenAI 签发，SAN 包含 `operator-proxy.invalid` 等 DNS 名，不包含目标 IP；与 ECS 提供的 Let's Encrypt IP 证书不同。因此这组失败发生在云端代理访问路径，不能据此认定 ECS 的 IP 证书配置错误。

没有绕过云端环境代理，没有跳过 TLS 校验。Dot 的云端 curl 与平台 OAuth 发现后端不一定使用同一访问路径，不能把一者的错误直接当作另一者的根因。

### 隔离平台重试和人工对照

网关新增过滤访问统计，只保存固定路由标签、时间、状态和耗时。请求对象、响应头、查询参数、客户端身份和正文在落盘前删除；授权、登录、Token 和其他路径跳过记录。日志为 0600，单文件上限 1 MiB，保留一个轮转文件。过滤规则已经用合成查询参数、Bearer 头和正文标记检查，标记未落盘。

另使用有时限的 TCP 443 连接统计，只保存时间与聚合计数，不保存数据包、来源地址或载荷。本人手动打开浏览器产生过一次 `/mcp → 401`，最初被误当作平台请求；本人指出后已撤回该判断，此记录不作为平台连通证据。

随后本人仅在平台重试两次，监测窗口没有新增入站连接或请求数据，也没有 OAuth HTTP 记录；之后人工 HTTPS 对照立即出现在相同统计中，元数据返回 200、MCP 返回 401。

再用临时地址 `https://203.0.113.10/mcp?discovery_check=20261019_a` 对照。人工探测确认该地址返回相同 OAuth challenge；本人在平台重试后，窗口仍只有事先人工对照的那条连接，没有新增平台请求。临时参数仅用于诊断，不作为正式 MCP 地址。该对照不能排除平台按 origin 等其他维度缓存失败。

各轮有时限的连接统计已结束；最后两轮在取得结果后主动停止，只终止本次创建的统计进程组，没有重启桥接或其他服务。

目前结论是这些平台操作没有向 ECS 发出可见的新请求。平台出网代理、IP 地址处理、发现逻辑、上游网络或缓存仍无法从服务器侧区分，不能宣称平台永久不支持纯 IP，也不能按错误字面认定服务端未实现 OAuth。

## 已实施的发现兼容调整

SDK 原有规范授权服务器发现地址返回 200，但部分斜杠变体返回 307 或 404；根路径资源元数据也未直接提供。HTTPS 网关现将规范地址及已覆盖的斜杠变体转发到同一份 SDK 元数据，并为发现响应设置 `Cache-Control: no-store`；资源发现同时支持根路径和 `/mcp` 路径。

此改动保留 OAuth、PKCE 和密码校验，仅重建 HTTPS 网关，没有重启桥接。Caddy 验证通过；当次完整回归 409 项，408 通过，1 项可选编解码测试跳过。后续平台发现仍失败，因此不能把这些兼容调整写成已解决平台问题。

网关原配置备份为 `data/config/https-Caddyfile.pre-discovery-20261019`。更细的未命中路径分类只曾在本地临时准备，之后撤回，没有部署到生产。

## 境外双向入口的讨论

本人明确说明大陆 ECS 没有完成域名备案，不接受继续把未备案域名指向该 ECS 的实验。没有用本次纯 IP 失败推导备案规则或备案拦截原因。

曾讨论把域名和 OpenAI 访问入口放在境外，同时转发两个方向：

```text
MCP 工具调用和 OAuth
OpenAI → 境外域名和 HTTPS 入口 → 加密链路 → ECS

微信事件触发 Dot
ECS → 加密链路 → 境外出口 → OpenAI 回调
```

所有公开 MCP 和 OAuth 地址需统一为境外域名，所有 OpenAI 事件回调从境外发出；微信通道和持久数据继续留在 ECS。只反代 `/mcp` 不足以实现这个目标。

计划优先评估 WireGuard 点对点链路。它仍是加密隧道，只在自有两机之间传输，不经过 Secure MCP Tunnel 服务。是否更快取决于实际跨境线路及各阶段耗时；没有测量结果，也没有部署境外入口或 WireGuard。

现有回调 `callback_proxy` 仅接受本人本机 `http://127.0.0.1:端口`，不能直接填入境外代理地址。完整双向方案需要部署配置和少量出站网络适配，保留目标白名单、TLS、公网目标校验与事件签名，不能直接开放任意代理。移除 ECS 现有通用代理客户端前，必须验证替代出口能投递事件。

## 保留状态和恢复入口

继续使用原 Tunnel 和唯一订阅，不切换 direct，不重新绑定微信，不清空 `data/`。HTTPS 网关、OAuth 配置及证书持久目录保留原状，URL 自动发现实验停止。不要为重试而重置密码、清空授权状态或扩大回调白名单。

以后恢复 URL 方向时，从以下两个分支选择一条：取得平台侧失败请求的具体诊断，或提供境外服务器与域名，先验证境外到 ECS 的线路和双向中转。只有 Dot 的 OAuth、工具与事件发现、订阅迁移、手机图片和文字收发都实际通过后，才关闭原 Tunnel。

与性能相关的后续测量应分别记录：微信接收、事件投递、Dot 开始处理、附件读取、回复提交和微信实际收到的时间。普通文字直接进事件已在 0.2.1 实施；附件按需下载和处理后正文清理仍需另行核验与完成，不纳入 URL 接入成功条件的替代证据。

## 相关文档

- [部署验收记录](../DEPLOYMENT_VALIDATION.md)
- [部署与接入步骤](../../DEPLOYMENT.md)
- [OpenAI OAuth 要求](https://developers.openai.com/plugins/build/auth)
- [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [公开 HTTPS 反代结构](https://developers.openai.com/plugins/build/mcp-server#deploy-the-endpoint)
- [WireGuard 协议说明](https://www.wireguard.com/)
