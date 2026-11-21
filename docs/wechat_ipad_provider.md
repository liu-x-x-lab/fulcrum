# 微信 iPad 874 接入说明

> provider 客户端不随本仓库发布；本文描述它与服务端之间的通信契约。

当前微信 iPad 渠道使用 `ipad-874` HTTP 契约。协议服务由平台统一部署和维护，企业租户只负责选择登录网络并扫码，不需要填写协议地址、Token、UUID、设备 ID 或接口参数。

协议依据：

- 源仓库：`ipad-874（私有仓库，未公开）`
- 当前锁定提交：`d9d3def46f8b5793e4272ba713fcce24c7df6267`
- 默认服务端口：`9064`
- 代理类型：仅 SOCKS

## 架构边界

平台侧负责：

- 部署和维护 874 服务。
- 在“系统运维 > 协议服务”维护服务地址和新账号接入状态。
- 在“系统运维 > 代理池”维护可分配的 SOCKS 代理。
- 观察服务日志、代理健康状态和平台审计。

企业租户负责：

- 创建微信 iPad 账号。
- 选择本次扫码使用的登录网络。
- 扫码并在手机微信中确认。
- 登录后使用消息、联系人和群聊能力。

租户页面不会展示或允许修改平台协议地址，也不会提供协议调用、二维码链接、CheckQR、探针和原始返回数据。

## 部署 874 服务

874 服务作为独立进程部署，不嵌入 SaaS 应用容器。部署完成后至少确认：

1. 服务监听预期地址和 `9064` 端口。
2. SaaS 应用容器可以访问服务根地址。
3. 874 Swagger 中的登录、心跳、消息、联系人和群聊接口与当前锁定契约一致。
4. 服务地址使用稳定的 HTTP 或 HTTPS 根地址，不带用户名、密码、查询参数或 URL 片段。

局域网示例：

```text
https://fulcrum.example.com
```

本项目不会生成 874 Token。当前契约没有 Token 鉴权字段，不应在租户配置或渠道 `adapter_config` 中自行增加无效凭证。

## 平台配置协议服务

平台员工登录平台后台后进入：

```text
系统运维 > 协议服务
```

填写 874 服务根地址，并选择“开放”或“暂停”新账号接入。保存后平台会把地址同步到现有微信 iPad 渠道；租户不能反向覆盖该地址。

暂停新账号接入不会创建新的连接草稿。已有账号是否继续运行由协议服务实际可用性和渠道状态决定。

## 平台配置代理池

平台员工进入：

```text
系统运维 > 代理池
```

可以手工新增代理，也可以导入 UTF-8 CSV。固定表头为：

```text
名称,省,市,主机,端口,用户名,密码
```

平台代理规则：

- 只分配已启用且健康的代理。
- 只精确匹配租户选择的省和市，不跨地区降级。
- 同一个代理同一时间只属于一个有效扫码会话。
- 取码前连接失败时在同地区轮换，最多尝试三个代理。
- 连续失败达到阈值后代理进入异常状态，检查通过后才能重新参与分配。
- 平台列表显示脱敏地址，不返回密码原文。

首版代理来源由平台员工维护，不依赖代理供应商接口。

## 租户扫码登录

首次接入和账号详情中的“重新登录”使用同一流程。

租户可以选择：

- 智能选择：选择省、市，由平台自动分配附近可用代理。
- 自有代理：填写自己的 SOCKS 主机、端口、用户名和密码。
- 直接登录：本次扫码不使用代理。

提交后页面自动准备二维码并轮询，只展示四类业务状态：

1. 准备二维码。
2. 等待扫码。
3. 请在手机确认。
4. 登录成功。

二维码生成后，本次会话固定当前代理，避免扫码与确认阶段发生 IP 变化。需要更换网络时点击“重新开始”，系统会取消旧会话、释放平台代理并生成新的二维码。

## 代理会话生命周期

登录代理不是渠道长期配置，不写入渠道 `adapter_config`。

- 每次扫码创建一个独立登录代理会话。
- 会话有效期以二维码有效期为准，并设置十分钟绝对上限。
- 登录成功、失败、取消、二维码过期或会话超时后立即释放平台代理。
- 自有代理密码在会话终态立即清除，只保留模式、地区和脱敏结果摘要。
- 下次登录可以重新选择地区、自有代理或直接登录。

普通心跳、消息同步、联系人同步和群聊同步不会继续使用扫码登录代理。

## Group directory

Open **Account details > iPad dashboard** to search friends, groups, members
and messages for one account. Both dashboards search stored friend records.
iPad sync follows the provider's contact cursors and fetches profiles in batches.
Missing profiles do not discard friend IDs or overwrite known names and avatars.
Records are scoped to the tenant, channel and signed-in WeChat account. The
database upgrade imports friends from previously saved iPad snapshots. Opening the dashboard
or the cross-account **Friends** directory does not send messages. Operators can
start a local direct conversation from either list; opening it again reuses the
conversation. Read-only users can open existing history. Both entries preserve
the source list filters and page when returning from the message center. Browsing
does not contact the protocol service. Operators can explicitly sync contacts
or groups; read-only users can browse. Group settings remain in **Group
details**, and conversation history opens in the message center.

**Sync groups** follows the signed-in account's contact pages to discover
groups, including groups that have never sent a message to Fulcrum. Names are
read in batches. Groups whose names are unavailable remain listed by ID.
An incomplete directory read does not apply partial results. Absence from the
directory does not mark a group as left or change its local settings.
Use **Sync current group** in group management to refresh the member roster.

Operators can edit or explicitly clear an iPad friend's remark in either
directory. The submitted value is stored only after a successful write and a
matching profile read for the same friend. An unconfirmed result is not retried;
sync the friend directory before deciding whether to submit another change.

**Friend requests.** The Friends directory and the iPad account dashboard open
the same inbox. It receives system friend requests from message sync, not
from manually entered IDs or credentials. Operators can accept a pending
request once; read-only users can see its status. A successful submission
is not a confirmed friendship. Sync friends to confirm that the applicant's
exact account ID appears in the current address book. An uncertain submission
is never retried automatically. Credential storage requires the configured
`ABOT_PLUGIN_SECRET_KEY`; without it, a request is visible but not actionable.

**Group announcements.** In Group management, open a group's **Profile**
and select **Sync group details**. This reads the announcement for that group;
opening the page or receiving messages does not fetch it. A failed read keeps
the last known text and fetch time. An explicitly empty announcement clears
the snapshot. This operation does not publish or edit announcements.

**Send a file.** Choose **File** in the message composer to send a nonempty
attachment up to 25 MiB. Standard plugin `file_reply(file_name=..., file_url=...)`
uses the same native attachment delivery; its public HTTP(S) URL must be
reachable from Fulcrum. File uploads need no public Fulcrum URL. A successful
upload alone does not confirm the message was sent. Check the conversation
before resending after a timeout or incomplete receipt.

**Send a video.** Select **Video** and choose a browser-playable MP4 up to
25 MiB. The composer prepares a JPEG cover and the duration locally. Plugins
use `video_reply(video_base64=clip, cover_base64=cover, play_length=seconds)`
with base64-encoded MP4 and JPEG or PNG data (cover limit: 2 MiB). The 874 implementation
that returns only the thumbnail upload receipt cannot confirm video delivery:
Fulcrum shows an unconfirmed result and does not automatically resend. Check
the destination conversation before submitting the clip again.

**Send a sticker.** Use the sticker button in the existing conversation and
choose a GIF, PNG or WebP file, up to 8 MiB. Plugins can return a standard
`emoji_reply(base64_data=...)`. Fulcrum sends the original bytes to the
provider's `UploadEmojiData` endpoint. The send is unconfirmed if the provider
does not return a matching final item with a native message ID. Check the
conversation before sending again.

Manual and plugin group invitations check the business return code and the
status of every requested member. A confirmed invitation is not proof of group
membership. After a timeout, partial response or mismatched recipient, check
WeChat before trying again. Invitations do not update the roster or send a
welcome card.

To change the actual WeChat group name, open **Group management > Group details >
Profile** as a tenant operator or administrator. This does not change the local
remark. The connected iPad account must be present in the current group roster
as the owner or an administrator. Fulcrum checks the current name before writing
and reads the same group again to confirm the new name. A timeout or inconclusive
result is not retried automatically; use **Sync current group** to check it.

Configure welcome cards under **Group management > Group features > Member change notifications**. The iPad and WeBridge channels use the same per-group settings. No plugin binding is required. For iPad, only identified members in a recognized inbound membership system notification can trigger a join or leave event. Plain text and roster refreshes do not send welcome cards.

Fulcrum matches group profiles by channel and group ID, not by name. Only a successful, versioned member response for the requested group with a positive count and a complete list of distinct personal IDs can mark absent members as having left. Invalid or stale responses leave the last accepted roster in place. Group settings and invitation history survive refreshes. A system notification can trigger a refresh, but is not evidence that its sender is a current member.

## 常见问题

### 所选地区没有可用代理

选择其他已开放地区，或改用自有代理、直接登录。系统不会自动跨省市分配。

### 自有代理无法取码

确认代理类型是 SOCKS，主机和端口可达，用户名和密码正确。页面不会返回 874 原始错误或代理密码。

### 二维码已经生成，但需要换代理

点击“重新开始”并重新选择网络。系统不会在已有二维码下热切换代理。

### 租户页面看不到协议服务地址

这是当前架构的正常行为。协议服务属于平台基础设施，只在平台后台“系统运维 > 协议服务”维护。

### 874 服务不可用

平台员工先确认服务进程、`9064` 监听、网络连通和 Swagger 契约，再恢复新账号接入。不要在租户渠道中增加备用地址或旧端点兼容配置。
