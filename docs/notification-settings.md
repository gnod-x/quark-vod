# Telegram 通知设置

打开管理台并使用管理令牌连接，在“通知”页完成设置；无需在终端编辑配置。管理令牌只保留在当前页面内存，刷新后需重新连接。

1. 在 Telegram 的官方 `@BotFather` 创建机器人，取得 Bot token。
2. 与自己的机器人打开私聊，发送 `/start`。个人接收者可向 `@userinfobot` 查询自己的数字 ID；群接收者应填写对应群的数字 Chat ID，并确保机器人有发消息权限。
3. 在“通知”页填写 Bot token 与 Chat ID，启用 Telegram，按需要开启观看提醒，点击保存。
4. 点击“发送测试通知”，再到 Telegram 确认收到消息。页面的成功提示只表示 Telegram API 接受请求，接收端仍需确认。

保存不会自动发送消息。Bot token 不回显；留空会保留服务器已有密钥，填写新值则更换。清除密钥前关闭 Telegram，并明确选择清除。测试使用已保存配置，每30秒最多一次，失败也会进入冷却。页面有未保存修改时，应先保存再测试；配置冲突时重新读取，以免覆盖另一管理页面的修改。

## 持久化与优先级

设置保存在共享 `VOD_DATA_DIR` 中的 `notification-settings.json`，权限为0600。服务与宿主作业每次发送前读取，保存后无需重启。管理接口只返回启用状态、是否已配置密钥、数字 Chat ID 和版本号，不返回 token；新通知凭据不写入浏览器存储、进程环境、`.env` 或只读搜索配置目录。

没有网页设置文件时，Telegram 与 P2 开关沿用现有显式配置：进程值优先于 `VOD_ENV_FILE`，包括显式空值；密钥支持对应 `_FILE`。不会自动发现 `.env`。首次网页保存可保留有效的环境密钥，之后文件对 Telegram 与 P2 开关具有优先权，网页停用不会回退到旧环境值。Bark、企业微信和通用 Webhook 仍使用原有配置；P2 开关决定所有通道是否发送观看类提醒。

文件采用跨进程锁、版本检查和原子替换；损坏、权限或路径检查失败时停止通知并返回受控错误，不用旧环境密钥绕过错误。锁文件 `notification-settings.lock` 与测试限频文件 `notification-test-rate.json` 同样为0600。备份共享数据时应将这些文件作为私密数据保存；回滚代码不能覆盖用户之后保存的设置。

通知与 Telegram 心愿单接收相互独立。配置通知不会启用机器人轮询或改变心愿单聊天白名单。

## 管理接口

所有接口均使用现有管理 Bearer 认证，并返回 `Cache-Control: no-store`。无令牌禁用管理。

| 方法与路径 | 行为 |
| --- | --- |
| `GET /api/admin/notifications` | 返回安全状态、来源与当前版本，不连接 Telegram |
| `PUT /api/admin/notifications` | 校验并保存设置，不发送消息 |
| `POST /api/admin/notifications/test` | 向已保存的 Telegram 接收者发送固定测试文本 |

保存请求包含 `revision`、布尔 `viewing_enabled`，以及 `telegram` 对象中的布尔 `enabled`、字符串 `chat_id`。可选 `bot_token` 留空或省略均保留已有密钥；`clear_token: true` 用于明确清除，不能同时提供新密钥。启用时必须有有效 token 和数字 Chat ID。测试请求仅包含 `revision`，不能指定任意消息、地址或接收者。

成功保存返回与 GET 相同的安全状态。测试成功仅返回 `accepted: true` 与 `channel: "telegram"`。请求格式错误返回400，版本冲突或未保存/未启用返回409，冷却返回429及 `Retry-After`，Telegram交付失败返回502，私密设置不可用返回503。错误正文只包含固定错误码，不回显请求或上游异常中的凭据。
