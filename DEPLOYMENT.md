# Docker + Tunnel 部署指南

文档更新日期：2026-10-19，Asia/Shanghai。部署和验收结果按实际执行时间另行记录。

本指南用于让 Agent 带着部署者，将微信本人私聊连接到已有的 Dot。服务器负责微信收发、持久队列和 MCP Events；模型能力由原 Dot 提供。当前分支版本为 0.2.4，共十三个工具。

按下面的顺序完成首次部署。所有服务器命令都在包含 `compose.yaml` 的项目根目录执行。已有可用部署时，先检查状态并复用绑定、Tunnel、密钥和订阅；已经通过的步骤不重复初始化或扫码。

## 先准备这些信息

| 准备项 | 如何取得或确认 | 通过条件 |
| --- | --- | --- |
| 部署主机和源码目录 | 部署者选定 Linux amd64 主机及项目目录，给 Agent 必要的终端访问 | 能在该目录运行 Docker Engine、Compose v2 和宿主 Python 3.11+ |
| 现有 Dot 和产品权限 | 用户登录目标 ChatGPT 工作区，确认私人 MCP 插件与 MCP Events 可用 | Agent 能指导用户找到对应入口；不能仅凭套餐名称假定可用 |
| Platform 组织和 ChatGPT 工作区 | 在 OpenAI 控制台与 ChatGPT 分别核对目标上下文 | Tunnel 关联包含实际使用 Dot 的工作区 |
| Tunnel ID | 在 [Platform Tunnels](https://platform.openai.com/settings/organization/tunnels) 创建或复用本人专用 Tunnel | 取得控制台给出的完整 `tunnel_id`，不自行编造 |
| Tunnel 运行 API Key | 在同一 Platform 组织下准备有 Tunnels Read + Use 的专用运行密钥 | 用户在私有 `.env` 中自行填写，或选择终端隐藏输入；密钥不经过聊天 |
| 微信 ClawBot | 用户确认微信端支持，并检查原 OpenClaw / cc-connect / 桥接是否仍使用同一绑定 | 同一绑定只有一个接收者，用户可以扫码及确认手机上的回复 |
| 可选的本机代理 | 仅在目标主机直连失败时，核对已有且由部署者管理的回环代理 | 已知用途和端口；构建、Tunnel 与 Events 回调分别配置 |

Agent 可以检查环境、准备不含秘密的配置、运行命令并解释结果。账号登录、私有 `.env` 中的密钥填写（或可选的隐藏输入）、微信扫码和手机验收由用户完成。只记录布尔状态、计数和固定错误类别，不把密钥、二维码、完整回调地址、数据库或私人正文贴进聊天、截图或文档。

### OpenAI Key 从哪里来、要什么权限

这是供 `tunnel-client` 连接 OpenAI 控制面的运行 API Key。桥接不直接调用模型，无需在这里配置模型名称或模型调用参数。

1. 用户打开 [OpenAI Platform](https://platform.openai.com/)，切换到管理该 Tunnel 的组织。先检查已有 Tunnel 和专用运行密钥，合适时复用。
2. 核对组织级权限：创建或编辑 Tunnel 的操作者需要 **Tunnels Read + Manage**；运行密钥和选择 Tunnel 的插件操作者需要 **Tunnels Read + Use**。仅有某个项目的权限不能自动推出具备组织级 Tunnel 权限。
3. 打开该组织可见的 [API Keys](https://platform.openai.com/api-keys) / 运行密钥入口，按 [官方 Tunnel 指南](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)和实际界面创建或取得专用运行密钥，并核验所授权限。不要为运行桥接直接使用全权限管理密钥。如果页面只提供项目权限，没有合适的运行密钥入口或 Tunnels 权限，请组织所有者 / RBAC 管理员协助；不要猜测某个普通项目 Key 一定可用。
4. 用户保管密钥，到第 3 步自行编辑本机私有 `.env`，也可继续使用隐藏输入。Agent 不索取密钥正文，不读取或输出真实 `.env`，不把密钥放进命令参数、Shell 历史或源码。已有运行密钥不会因为重建容器而需要重新创建。

权限和组织 / 工作区关联依据 [OpenAI 官方 Tunnel 文档](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)及 [RBAC 说明](https://developers.openai.com/api/docs/guides/rbac)。控制台入口与账号权限以当前实际可见状态为准；这里不承诺每个账号都已开放 Tunnel、私人插件或 Events。

## 1. 检查主机和目录

Agent 先执行只读检查：

```sh
uname -m
python3 --version
docker version
docker compose version
id -u
id -g
```

**通过条件：** Linux amd64（通常显示 `x86_64`），Docker daemon 可访问，Compose v2 可用，源码及 `compose.yaml` 已就位。宿主 Python 用于下载脚本，运行依赖由 Docker 镜像按锁文件安装。

容器默认 UID/GID 为 1000:1000。用普通部署用户管理专用目录；若实际 UID/GID 不同，把 `RUN_UID`、`RUN_GID` 合并写入本机被 Git 忽略的 `.env`，保持后续 Compose 命令一致。不要覆盖已有 `.env`。`data/` 应属于同一部署用户且为 0700，不能靠开放权限解决访问失败。

```sh
sh scripts/manage.sh init
```

已有 `data/` 时先保留它并核对属主；不要删除后重新部署。原绑定还在其他程序或主机运行时，先与部署者确认单一接收者的切换安排。

## 2. 构建并启动桥接容器

没有代理需求时：

```sh
python3 scripts/fetch-tunnel.py
docker compose build bridge
docker compose up -d bridge
curl --noproxy '*' -sS http://127.0.0.1:8788/healthz
```

下载脚本固定并校验官方 tunnel-client v0.0.16 的归档 SHA256，包含伴随程序和许可；镜像与 Python 依赖也固定版本。按本仓库已验证版本构建，不直接换成官方文档中的最新二进制或另装 systemd 服务。

如果下载或构建需要已有本机代理，只替换该阶段的命令；示例端口必须改成部署者自己的端口：

```sh
HTTPS_PROXY=http://127.0.0.1:10800 python3 scripts/fetch-tunnel.py
BUILD_HTTP_PROXY=http://127.0.0.1:10800 docker compose build bridge
docker compose up -d bridge
```

**通过条件：** `/healthz` 返回 200 且 `process_healthy:true`。首次尚未配置 Tunnel 时，`mode:waiting_for_tunnel` 和 `/readyz` 返回 503 属于准备中的正常状态，不代表已经接入 Dot。

容器根文件系统只读，桥接限制为 512 MiB 内存和 1 核 CPU；`data/` 是持久 bind mount。Linux host 网络用于复用宿主回环代理，健康端口 8788 只监听回环。此部署路线不需要公网 MCP 入站端口。

## 3. 手工编辑 `.env`：Tunnel、Key 和运行代理

在 [Platform Tunnels](https://platform.openai.com/settings/organization/tunnels)确认 Tunnel 所属组织及关联的目标 ChatGPT 工作区。只关联需要使用它的上下文，并核验实际可访问者；本项目面向本人单账号，不用于共享多用户服务。

默认推荐自行编辑 dotenv，不需要在终端粘贴运行 Key。部署时各自负责的内容如下：

| 环节 | 由谁完成 |
| --- | --- |
| 准备 `.env` | Agent 可检查文件是否存在；仅在不存在时复制仓库的 [.env.example](.env.example)，并设置为 0600。已有 `.env` 不覆盖 |
| 填写或更新配置 | 用户用自己的编辑器填写 Tunnel ID、运行 Key 和所需代理 / DNS；已有文件缺少变量时，也由用户自行补入。Agent 不读取实际 `.env`、不代填秘密 |
| 加载配置 | Docker Compose 读取 `.env`，把变量传入容器；桥接程序读取这些环境变量，不会自动创建、修改 `.env`，也不会把磁盘上的旧配置或 Key 回填到 `.env` |
| 生成运行文件 | 桥接启动时验证非空的新值，再自动生成或更新 `data/` 中对应的私有运行文件 |

首次准备 `.env` 可执行：

```sh
if [ ! -e .env ]; then cp .env.example .env; fi
chmod 600 .env
```

用户用自己的编辑器填写，Agent 不打开实际密钥文件。下面仅展示结构，Tunnel ID 与 Key 的占位值必须替换，代理按实际需要填写：

```dotenv
WEIXIN_TUNNEL_ID=tunnel_YOUR_ID
WEIXIN_TUNNEL_RUNTIME_KEY='YOUR_RUNTIME_KEY'
WEIXIN_TUNNEL_PROXY=
WEIXIN_CALLBACK_PROXY=
WEIXIN_CALLBACK_DNS=
WEIXIN_MEDIA_PROXY=
WEIXIN_MEDIA_DNS=
```

| `.env` 变量 | 作用 / 可填值 |
| --- | --- |
| `WEIXIN_TUNNEL_ID` | Platform 给出的完整 Tunnel ID |
| `WEIXIN_TUNNEL_RUNTIME_KEY` | 专用运行 Key；用单引号包裹，防止 `$` 等字符被 Compose 插值 |
| `WEIXIN_TUNNEL_PROXY` | Tunnel 到 OpenAI 控制面的代理，例如 `http://127.0.0.1:10800` |
| `WEIXIN_CALLBACK_PROXY` | Events 回调代理，仅允许已有的本机回环 HTTP 代理 |
| `WEIXIN_CALLBACK_DNS` | `system` 或 `google` |
| `WEIXIN_MEDIA_PROXY` | 腾讯媒体 CDN 代理，规则同回调代理；微信 API 仍直连 |
| `WEIXIN_MEDIA_DNS` | `system` 或 `google` |

**配置优先级：** Compose 读取项目根目录的 `.env` 并传给桥接；同名 Shell 环境变量按 Compose 规则有更高优先级，避免遗留导出值覆盖文件。启动时，非空 `WEIXIN_*` 值覆盖相应的已保存字段；空值 / 未填写保留 `data/` 中已有配置。所有新值先验证，通过后才写入私有运行文件；配置无效时不覆盖已有设置。

这里的“已有配置”明确指：

- `data/config/tunnel.yaml`：Tunnel ID、控制面代理及 Key 的文件引用。
- `data/secrets/runtime.key`：实际运行 Key，0600；Tunnel 配置不保存 Key 正文。
- `data/config/bridge.json`：回调白名单、回调 / 媒体代理和 DNS 等桥接设置。

`data/config/tunnel.yaml` 和 `data/secrets/runtime.key` 仍是 Tunnel 的实际运行依赖。首次部署在 `.env` 填好 Tunnel ID 和运行 Key 后，桥接会自动生成这两份文件，无需手工创建；日后也需保留它们，不能因为已经填写 `.env` 就删除。

首次部署没有旧值时，代理默认直连，DNS 默认 `system`；Tunnel ID 和运行 Key 仍需提供。代理变量填 `direct` 表示明确删除已保存代理并改为直连，DNS 填 `system` 可恢复系统 DNS。把 Key 留空不会删除旧 Key；手工改磁盘配置后，如果 `.env` 中仍有同名非空值，下次启动仍以 `.env` 为准。`data/` 同时承载绑定、订阅与队列，不能为重配 dotenv 而清空。

保存 `.env` 后重新创建桥接容器，使 Compose 传入新环境；单纯 `restart` 不会重新读取 dotenv：

```sh
docker compose up -d --force-recreate bridge
curl --noproxy '*' -sS http://127.0.0.1:8788/readyz
```

本功能需要按当前源码构建的镜像；更新源码后先按第 2 步构建，再执行上述命令。容器会把设置合并到私有运行文件，保留微信绑定、订阅和白名单，Tunnel 子进程继续通过文件读取 Key。

**通过条件：** `mode:tunnel`、`process_healthy:true`、`tunnel_ready:true`。仅保存 `.env` 不代表连通；必须实际看到 Tunnel ready。此时未绑定或没有订阅，整体 `ready:false`、HTTP 503 仍符合预期。

`.env` 是明文私有文件，已被 Git 和 Docker 构建上下文排除；不要分享其内容或包含值的 `docker compose config`、`docker inspect`、环境变量转储。Compose 会把运行 Key 注入容器环境，拥有 Docker 管理权限的人仍可查看；若希望只保留文件式密钥，将对应 dotenv 值留空，使用下面的兼容方式。dotenv 的读取与优先级见 [Docker 官方说明](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation)。

### 可选：保留终端隐藏输入方式

不在 `.env` 填写 Tunnel ID / Key 时，也可沿用已有配置或首次通过命令准备。把占位 ID 替换为控制台值；需要控制面代理时在末尾加上本人本机代理地址：

```sh
docker compose exec -T bridge weixin-mcp prepare-tunnel tunnel_YOUR_ID
sh scripts/manage.sh tunnel
```

第二条由用户在交互终端隐藏输入；空输入保留已有 Key。此方式与 dotenv 可以共存，但 `.env` 中同名非空字段在下次容器启动时优先。

如果 Tunnel 在插件页面不可见，先检查工作区关联与操作者 Read / Use 权限；组织权限由组织所有者 / RBAC 管理员处理，ChatGPT 插件访问由工作区权限决定。

## 4. 微信扫码绑定，先保持接收暂停

已有有效绑定时复用，跳过扫码。首次扫码前确认同绑定没有其他接收者；脚本会停止桥接容器：

```sh
sh scripts/manage.sh login
```

由用户通过可信的本机或 SSH 文件访问私下查看 `data/state/weixin-login-qr.png`，在微信扫码确认。二维码不上传到聊天或公开页面。随后查询：

```sh
sh scripts/manage.sh login-poll
```

尚在等待时可再次查询；过期时重新执行登录步骤。如果返回 `requires_phone_verification:true`，由用户在交互终端隐藏输入手机验证码：

```sh
docker compose run --rm bridge login-poll --verification-code
```

**通过条件：** 登录返回 `status:confirmed`，绑定已写入磁盘。`already_bound` 不等于新登录成功，遇到它应检查已有绑定，不能清空数据库重来。

先启动桥接以供插件连接，此时保持微信接收暂停：

```sh
docker compose up -d bridge
curl --noproxy '*' -sS http://127.0.0.1:8788/readyz
```

首次扫码后预期 `bound:true`、`enabled:false`；尚未恢复接收时整体 ready 不成立。先完成下两步的插件与订阅配置，再执行 `resume`，以免新来信早于新订阅建立。

## 5. 在已有 Dot 中连接私人插件

1. 用户打开 [ChatGPT Plugins](https://chatgpt.com/plugins)，在目标工作区创建私人 MCP 插件，Connection 选 **Tunnel**，选择第 3 步的同一个 Tunnel。
2. 本方案的认证选择 **No authentication / 无认证**，访问控制依赖本人专用 Tunnel 和私人插件的实际权限；不要分享插件或发布到公共目录。
3. 扫描工具和事件，确认十三个工具与 `weixin.message` 出现，再把插件添加到原有 Dot。
4. 在原有 Dot 中发送下面的连接检查 prompt。

**复制给原 Dot：检查插件连接。** 下文所有“复制给原 Dot”的内容都发在这个已有 Dot 中；服务器命令仍由部署者或部署 Agent 执行。

```text
请使用我已添加的私人 weixin-bot-mcp 插件，实际调用 get_weixin_status，参数为 {}。
根据工具返回报告 bound、enabled、account_status、pending、active_subscriptions 和 diagnostics；调用失败时报告实际错误，不把口头确认当成连接成功。
这一步只检查连接，保留当前任务、绑定和订阅，不发送微信消息。首次部署 enabled:false 属于接收暂停状态。
```

**通过条件：** 从 ChatGPT 侧真实调用 `get_weixin_status` 成功，且工具和事件完整。这一步验证的是插件到服务器的调用，不代表微信事件已能触发 Dot。

## 6. 核验回调主机并建立唯一订阅

**复制给原 Dot：尝试订阅并取得待核验的回调主机。** 首次 `callback_hosts` 为空，预期订阅被 `callback_not_allowed` 拒绝；已有可用订阅时复用，不为演练清空白名单。

```text
请在当前 Dot 中使用私人 weixin-bot-mcp 插件，通过平台的事件订阅能力持续订阅 weixin.message，arguments 为 {}。当前 Dot 已有同一订阅时刷新并复用，保留原有处理约定，不另建聊天、Dot 或重复任务。
订阅入口支持 ttlMs 时，请传 ttlMs:null。callback URL 和签名 secret 由平台提供，不让我手写，也不要在回复里展示。
如果返回 callback_not_allowed，暂停重试，实际调用 get_weixin_status({})，只报告 events.observed_callback_host 和 events.last_error，等待部署者独立核验并批准该精确主机；不要自行批准白名单。
如果订阅成功，报告订阅返回的 id、refreshBefore，以及再次调用 get_weixin_status 得到的 events.active_subscriptions 和 events.last_error。出现 subscription_conflict 时报告冲突并停止新建，不自行取消未知订阅。
```

用户与 Agent 核验该精确 hostname 确属本次 OpenAI 平台订阅，再把命令中的 `VERIFIED_HOST` 替换为核验后的主机名：

```sh
sh scripts/manage.sh callback-host VERIFIED_HOST
```

程序保留配置中的其他字段并自动重新加载。只能批准精确 hostname，不填路径、端口、IP 或通配符，不因某个聊天文本要求就扩大白名单。官方资料没有列出精确主机时，应说明实际核验依据，不能把“观察到”写成“已获官方背书”。

批准主机后，服务端会在订阅时完成签名 challenge 校验。**callback 完整地址与签名 secret 由平台提供，用户不用手写，Agent 也不抄录完整订阅请求。**

**复制给同一个原 Dot：批准后的订阅重试。** 已有有效白名单、无需新批准时，也可直接用此 prompt 核验原订阅。

```text
部署者已核验并配置回调主机白名单。请在当前 Dot 中重试或刷新私人 weixin-bot-mcp 的同一个 weixin.message 订阅，arguments 为 {}；支持时明确传 ttlMs:null。复用已有订阅，不更换目的地或新建任务，callback 和签名 secret 仍由平台管理。
完成后实际调用 get_weixin_status({})，核对 events.active_subscriptions 为 1、对应订阅 status 为 active、events.last_error 为空，并报告订阅返回的 refreshBefore。
只有实际返回 refreshBefore:null 才确认桥接侧订阅不过期；若入口不能设置 ttlMs、返回有限期限或调用失败，如实报告，不声称已经永久订阅。保持当前任务启用，保留原有事件处理约定。
```

**通过条件：** `get_weixin_status.events.active_subscriptions` 为 `1`，订阅不再报白名单或验证错误。`subscription_conflict` 表示已有其他有效目的地；先核对占用订阅的原任务。只有已确认该旧任务应停止时，才把下面的 prompt 发到**持有旧订阅的 Dot**，完成后回到目标 Dot 使用上面的订阅重试 prompt。

```text
我要停止当前 Dot 对私人 weixin-bot-mcp 的旧订阅。请通过平台取消当前 Dot 持有的 weixin.message 订阅，arguments 为 {}，只处理这个插件和事件。不要删除微信绑定、历史或服务器配置，也不要在这里重新订阅。
随后实际调用 get_weixin_status({}) 并报告 events.active_subscriptions。只有结果为 0 才确认通道已释放；仍有有效订阅、取消失败或无法确认归属时，如实报告，不取消其他未知任务。
```

上面的订阅 prompt 已请求 `ttlMs:null`。如果客户端未暴露该参数，在唯一订阅激活后由部署者执行：

```sh
docker compose stop bridge
docker compose run --rm bridge subscription-forever
docker compose up -d bridge
```

**执行兼容命令后，复制给同一个原 Dot：刷新原订阅。**

```text
部署者已把当前唯一订阅在桥接侧设为不过期。请通过平台刷新当前 Dot 已有的 weixin.message 订阅，arguments 为 {}；入口支持时仍传 ttlMs:null，不另建订阅或任务。
报告本次订阅响应中的 refreshBefore，并实际调用 get_weixin_status({})，核对 events.active_subscriptions 为 1、该订阅 refreshBefore 为 null，保持当前任务启用。无法刷新或平台仍给出有限期限时如实报告，不把服务器命令成功当成平台任务永久存在。
```

桥接侧不过期不等于平台任务永久存在；任务停用、取消订阅和 410 回调仍会停止投递。平台创建、刷新和取消订阅的机制见 [OpenAI 官方 MCP Events 文档](https://developers.openai.com/plugins/build/mcp-events)。

## 7. 恢复接收并完成真人验收

**先复制给原 Dot：持续处理微信来信。** 此 prompt 为第 6 步的已有订阅补充处理约定，不新建第二份订阅；配置完成后再恢复接收。

```text
请保留当前任务和私人 weixin-bot-mcp 的唯一 weixin.message 订阅，保持任务启用。收到事件后按下面的约定处理我从绑定微信发来的请求：
1. 使用事件 data.message_id 关联同一条来信。text_complete:true 时直接处理事件 text，不额外调用 read_weixin_message；text_complete:false 或缺少正文时调用 read_weixin_message(message_id)，按返回的 next_offset 分页，直到读完。恢复待处理来信时也先读取内容。
2. attachments 只是元数据，需要内容时调用 read_weixin_attachment(attachment_id)；attachment_pending 时稍后读取同一 ID。只根据实际读取的文字、图像或音频作答，读取失败如实说明。
3. 用 reply_weixin_message 回复到同一 message_id。同一来信可以先发 final:false 的进度，再发 final:true 的最终结果；长任务用 report_weixin_progress(message_id, active:true) 续期。工具给出 processing_id、revision 时，后续同一轮处理沿用它们。同一次预期回复保持稳定 reply_key，新的一次回复使用新 key。
4. 保存每次回复返回的 reply_id，用 get_weixin_send_status(message_id, reply_id) 查询这一条回复，queued 时稍后查询。accepted 只表示微信 API 受理，只有我确认手机收到后才记录实际送达。遇到 uncertain 不换 ID、reply_key 或正文重发。
5. 只有我明确要求把其他任务的结果通知到微信时，才使用 send_weixin_notification；同一次通知保持稳定 notification_id，并用 get_weixin_notification_status 查询。不向其他人发送，不自动转发未授权的其他对话。
```

确认原绑定没有第二个接收者，且上面的处理约定已交给原 Dot，再由部署者恢复接收：

```sh
sh scripts/manage.sh resume
curl --noproxy '*' -fsS http://127.0.0.1:8788/readyz
```

**服务就绪条件：** `/readyz` 返回 200，`ready:true`、`mode:tunnel`、`tunnel_ready:true`、`bound:true`、`enabled:true`、`account_status:ready`、`active_subscriptions:1`。

普通文字（最多 16 KiB UTF-8）直接随事件传入；长文的 `text_complete:false` 要求按 `next_offset` 读取全文，单页最多 64 KiB。附件先发元数据，Dot 按需读取时才下载；`attachment_pending` 时稍后读取同一 ID。回复全部受理且任务完成后，正文自动清空。

最后按下面的顺序完成手机验收。

**在绑定的微信私聊发送，不是发到 Dot 页面：**

```text
微信部署验收：请只回复“微信通道已连接”。
```

原 Dot 应被事件触发，按上面的处理约定提交回复并查询对应发送状态。先核对 `accepted`，再由用户确认同一微信对话实际收到“微信通道已连接”。

**需要附件能力时，复制给原 Dot，再从微信发送测试材料：**

```text
保留当前任务和唯一订阅，接下来分别验收微信附件。我会从绑定微信发送图片、小文本文件或语音，并在微信说明处理要求。
请通过 read_weixin_attachment 读取实际内容后处理：图片描述你实际看到的内容；文本文件说明实际读到的内容；语音优先使用实际提供的转写，只有具备真实可用的识别能力时才补充识别，否则说明限制。不要把附件元数据当成已经读取内容。
按微信中的要求回复到对应 message_id。若我要求回传已有附件，使用该 attachment_id 调用 reply_weixin_attachment。检查每次回复的 reply_id 状态，并等我确认手机结果；图片、文件和语音分别记录，某一项通过不代表其他项通过。
```

**需要主动通知能力时，复制给原 Dot：** 这段 prompt 明确授权一次测试通知。

```text
请通过私人 weixin-bot-mcp 向我绑定的微信主动发送一次测试通知，正文为“微信主动通知测试”。为这一次测试生成并保存一个稳定 notification_id，调用 send_weixin_notification(notification_id, text)，再用同一个 ID 调用 get_weixin_notification_status 查询。
queued 时稍后查询；context_required 时说明需要我先从微信发一条新消息，再按工具状态处理同一个通知，不另建通知；uncertain 时停止重发并让我核对手机。accepted 只表示 API 受理，等我确认微信实际收到后才记录测试通过。
```

只通过到哪项就记录到哪项。Tunnel ready、订阅成功、自动化测试或 API `accepted` 都不能单独作为微信收发闭环完成的证据。

## 可选网络配置：按请求方向分别处理

| 方向 | 配置位置 | 何时需要 |
| --- | --- | --- |
| 下载及 Docker 构建 | 下载命令的 `HTTPS_PROXY`、Compose 构建的 `BUILD_HTTP_PROXY` | GitHub、镜像仓库或包下载失败时排查；Docker daemon 拉取镜像的网络另行检查 |
| Tunnel 到 OpenAI 控制面 | `.env` 的 `WEIXIN_TUNNEL_PROXY` | Tunnel 无法直连 OpenAI 时 |
| Events 到平台回调 | `.env` 的 `WEIXIN_CALLBACK_PROXY` / `WEIXIN_CALLBACK_DNS` | Tunnel 工具调用成功，但回调验证或投递发生网络错误时 |
| 媒体到腾讯 CDN | `.env` 的 `WEIXIN_MEDIA_PROXY` / `WEIXIN_MEDIA_DNS` | 仅在附件下载或媒体网络确有问题时；微信 API 仍直连 |

例如只需调整回调网络时，用户在 `.env` 合并下面两项并按实际端口修改，再重新创建桥接容器：

```dotenv
WEIXIN_CALLBACK_PROXY='http://127.0.0.1:10800'
WEIXIN_CALLBACK_DNS=google
```

已有 `data/config/bridge.json` 的 `callback_proxy` / `callback_dns`、`media_proxy` / `media_dns` 配置继续兼容，dotenv 的非空值会在启动时覆盖对应字段。代理只允许部署者管理的回环 HTTP 代理；按实际需要配置，不把某台机器的端口当通用默认。

`BUILD_HTTP_PROXY` 也可写在 `.env`，供 Compose 构建参数读取；直接运行宿主 `python3 scripts/fetch-tunnel.py` 不会读取 dotenv，仍按第 2 步给该下载命令设置 `HTTPS_PROXY`。构建代理不会自动成为运行代理。保持精确回调白名单、公共 IP 连接校验和 TLS 验证，详见 [安全边界](docs/SECURITY.md)。

## 常见卡点

| 观察到的状态 | 下一步 |
| --- | --- |
| `waiting_for_tunnel` | 检查 dotenv 填写及容器是否已重新创建；只核验私有运行文件存在、权限和就绪状态，不输出密钥 |
| `tunnel_ready:false` | 核对 Tunnel ID、运行权限、控制面出站网络；插件不可见时另核对工作区关联 |
| `binding_required` 或 `bound:false` | 完成微信绑定；不要提前把订阅或其他工具的失败归因于 Key |
| `callback_not_allowed` | 按第 6 步核验并批准精确主机，然后重试同一订阅 |
| 回调 TLS / 超时错误 | 单独检查 Events 出站网络，不以 Tunnel ready 推断回调可达 |
| `/readyz` 为 503 | 按当前步骤检查绑定、启用状态、Tunnel 和唯一订阅；准备阶段不要求整体 ready |
| 通知 `context_required` | 等待本人新来信提供有效 context；不能承诺永久无条件主动推送 |
| 发送 `uncertain` | 用户核对微信实际结果；不换 ID 或自动重发 |

修复订阅或网络后，需要恢复未完成来信时，**复制给原 Dot：**

```text
请保留当前唯一订阅，先调用 get_weixin_status({})，报告绑定、订阅和固定错误类别。状态允许继续时，调用 list_pending_weixin_messages，逐条用 read_weixin_message 读取未完成来信，并用 get_weixin_send_status 检查已有回复，再按来信要求继续处理。
已发 final:false 进度的任务仍可能未完成，可以继续；已经受理的回复不要重复发送，uncertain 的回复不要重试。读取结果 text_cleared:true 时说明正文已清理，不编造历史内容。不要为恢复任务创建另一份订阅或重新绑定。
```

## 磁盘持久化与日常检查

| 路径 | 内容 |
| --- | --- |
| `.env` | 用户手工编辑的私有配置入口，0600，包含 Key 时不读取或展示其内容 |
| `data/config/bridge.json` | 私人桥接配置、精确 callback 主机白名单和可选网络设置 |
| `data/config/tunnel.yaml` | Tunnel 配置和运行密钥的文件引用（JSON 形式的有效 YAML） |
| `data/secrets/runtime.key` | 从 dotenv 导入或由用户隐藏输入的运行密钥，0600 |
| `data/state/bridge.sqlite3` 及 WAL/SHM | 绑定、游标、消息、回复、通知、订阅与附件 |
| `data/state/weixin-login-qr.png` | 私下扫码用的登录二维码 |
| `data/locks/`、`data/home/` | 进程锁和 Tunnel 本地运行目录 |

`data/` 整体保留在宿主磁盘，专用目录 0700、私有文件 0600，不进入 Git。重建容器继续复用它；备份时停止服务，通过 SQLite backup 或项目备份命令生成一致快照。恢复、升级和不确定发送的处理见 [运维说明](docs/OPERATIONS.md)。

```sh
sh scripts/manage.sh status
curl --noproxy '*' -sS http://127.0.0.1:8788/healthz
curl --noproxy '*' -sS http://127.0.0.1:8788/readyz
docker compose logs --tail 30 bridge
sh scripts/manage.sh test
```

测试使用独立 test 镜像及临时文件系统，不挂载生产 data，也不联系真实微信账号；构建阶段需要下载测试镜像依赖，可选编解码器缺失时对应测试会跳过。自动化测试不代替本次部署的手机验收。

## 主动通知的边界

- `send_weixin_notification(notification_id, text, text_format?)`：无需入站 message_id，只发给扫码绑定者，不接受任意收件人。
- `get_weixin_notification_status(notification_id)`：查询持久状态。相同 ID、正文重复调用返回原记录，正文变化被拒绝。
- 长文有序分段；与普通回复共享 32 条待发上限和串行发送 worker，普通回复优先。
- 缺少有效 context 或微信明确拒绝旧 context 时为 `context_required`，等待本人新来信恢复；不承诺永久无限推送。
- 超时、响应不明或发送中途重启保留为 `uncertain`，不自动重发；`accepted` 仅表示 API 接受，不等于手机已收到。
- 通知不伪造来信、不触发自身 Events，也不改变原来信任务的完成状态。

## 可以交给 Agent 的部署任务说明

> 阅读本仓库 DEPLOYMENT.md，按 Docker + Tunnel 路线指导我把微信本人私聊接到已有 Dot。先检查目标主机、目录、普通部署用户、Platform 组织、ChatGPT 工作区和产品权限，复用已有 Tunnel、绑定、运行密钥和订阅；只在首次部署或已确认需要时执行初始化。检查 .env 是否存在，仅在不存在时复制不含秘密的 .env.example 模板并设置 0600，保留已有文件。让我自行登录、创建或取得专用运行 Key、用自己的编辑器私下填写 .env 中的 Tunnel ID、运行 Key 和所需代理 / DNS（也可选择终端隐藏输入）、扫码和确认微信收件；不要读取实际 .env、代填秘密、索取或输出任何秘密。说明 Compose 读取 .env，桥接只把环境变量导入 data 中的运行文件，不会自动修改或回填 .env；首次填好 ID 和 Key 后无需手工创建 tunnel.yaml 和 runtime.key，但这两份文件仍需保留。每次修改 .env 后重新创建 bridge 容器，不能只 restart。依次验证容器存活、Tunnel ready、微信绑定且接收暂停、插件工具与事件发现、真实状态调用、回调主机核验和唯一订阅，再恢复接收并做文字、所需附件与主动通知验收。需要我在原 Dot 中操作时，逐步提供本文对应的完整可复制 prompt，说明应发给哪个 Dot、何时发送和预期结果，不只说“让 Dot 调用”或“让 Dot 订阅”；微信端测试文字另行标明。每一步说明观察结果，失败时保留现有 data 并从该检查点排查；不要另起第二个接收者、扩大白名单或重发 uncertain。只有用户确认微信实际收到对应回复后，才把该项闭环标为通过。结果只记录版本、布尔状态、计数、固定错误类别和尚未完成的项目。
