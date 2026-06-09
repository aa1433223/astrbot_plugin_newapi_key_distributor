# NewAPI Key 分发组件

AstrBot 的 NewAPI Key 管理与分发插件，面向 AstrBot/QQ 用户提供 Key 绑定、申请、审批、自动创建和管理员主动发放能力。

默认 NewAPI 地址：

```text
https://newapi.qianye.host/
```

## 功能

- `/key 填写 sk-xxx`：用户绑定已有 Key，并调用 NewAPI 用量接口做有效性校验。
- `/key 申请 用途说明`：提交申请，等待管理员审批。
- `/key 创建 [用途说明]`：开启自助创建时直接创建；未开启时自动转为申请。
- `/key 查看`：查看自己的 Key 记录。
- `/key 用量 [记录ID]`：查询用量。默认不保存完整 Key，因此需要开启 `store_plain_keys` 后重新绑定才可用。
- `/key 删除 <记录ID>`：删除/停用本地记录，可按配置同步删除远程 token。管理员也可按 QQ 删除该用户所有 active Key。
- `/key 审核`：管理员查看待审核申请。
- `/key 通过 <申请ID> [姓名] [金额]`：管理员审批并自动创建 NewAPI token，分组和有效期使用默认值。
- `/key 生成 <QQ> <姓名> [金额]`：管理员主动给指定用户发放 Key。QQ 和姓名必填，金额可省略。
- `/key 加额 <记录ID|QQ> <金额>`：管理员给已有 Key 增加额度，并同步 NewAPI 远程 token。
- `/key 拒绝 <申请ID> 原因`：管理员拒绝申请。
- `/key 封禁 <QQ>`、`/key 解封 <QQ>`：管理员控制用户状态。
- `/key 检查`：管理员检查 NewAPI 管理接口配置是否可用。
- `/key 配置 ...`：管理员私聊写入 NewAPI 地址、Access Token、用户 ID 和组件管理员。

组件管理员拥有额外权限：

- 发放/审批创建不受 `max_keys_per_user` 限制。
- `/key 查看` 查看所有 active Key，`/key 查看 <QQ>` 查看指定用户。
- `/key 删除 <记录ID>` 删除任意记录，`/key 删除 <QQ>` 删除该用户所有 active Key。
- `/key 修改 <记录ID|QQ> [姓名] [金额]` 修改任意 Key，并尽量同步 NewAPI 远程 token。
- `/key 修改名称 <记录ID|QQ> <名称>` 单独修改名称。
- `/key 修改分组 <记录ID|QQ> <分组>` 单独修改分组。
- `/key 修改金额 <记录ID|QQ> <金额>` 单独修改金额额度。
- `/key 加额 <记录ID|QQ> <金额>` 给任意 Key 追加金额额度，并同步 NewAPI 远程 token。
- `/key 用量 <记录ID|QQ>` 查询任意记录用量。

命令别名：

```text
/key
/apikey
/newapi
```

## 配置重点

必须配置：

```text
newapi_base_url = https://newapi.qianye.host/
admin_access_token = NewAPI 后台生成的访问令牌
admin_user_id = 你的 NewAPI 管理用户 ID
bot_admin_ids = 允许审核的 QQ 号列表
```

也可以由管理员私聊机器人写入运行时配置：

```text
/key 配置 token <Access Token>
/key 配置 user <NewAPI用户ID>
/key 配置 url https://newapi.qianye.host/
/key 配置 管理员 添加 <QQ>
/key 配置 查看
/key 配置 刷新
```

如果 `bot_admin_ids` 还没有配置，插件允许第一个私聊执行 `/key 配置 ...` 的人完成初始化。聊天命令会直接写入 AstrBot 插件配置，和面板里的同名配置使用同一份数据。面板改完后可执行 `/key 配置 刷新` 让插件重新读取当前配置；旧版本的 `runtime_config` 会在启动时自动迁移到插件配置。

常用策略：

```text
auto_create_enabled = false
enable_chat_config = true
private_only_for_secret = true
store_plain_keys = false
max_keys_per_user = 1
default_group = 浅夜の梦专属号池
default_amount = 1
quota_per_amount_unit = 500000
default_expire_days = 0
default_model_limits =
```

`store_plain_keys` 默认关闭。关闭时，本地只保存脱敏 Key 和 token id；完整 Key 只会在创建或审批通过时出现一次。开启后可以查询绑定 Key 的用量，但本地数据文件会保存明文 Key，请自行评估风险。

金额参数使用实际金额，不直接填写 NewAPI 原生额度。插件会用 `金额 * quota_per_amount_unit` 换算为 NewAPI 的 `remain_quota`。默认 `quota_per_amount_unit = 500000`，如你的站点换算不同，请在配置里调整。

完整 Key 和 Access Token 不会在群聊展示。若在群聊执行会返回提示，请私聊机器人重新执行。

## NewAPI 权限

插件使用 NewAPI token 管理接口：

- `POST /api/token/`
- `GET /api/token/search`
- `GET /api/token/`
- `POST /api/token/{id}/key`
- `DELETE /api/token/{id}`
- `GET /api/usage/token`

新版 NewAPI access token 通常需要同时提供：

```text
Authorization: Bearer <admin_access_token>
New-Api-User: <admin_user_id>
```

## 第一版限制

- 当前采用“统一 NewAPI 管理账号给每个 QQ 用户创建独立 token”的模式。
- 审批通过后，完整 Key 会返回给管理员；组件暂不强依赖 AstrBot 私聊主动发送能力。
- 默认不会保存完整 Key，因此用户丢失后建议删除记录并重新创建。
- `default_quota` 的单位取决于你的 NewAPI 站点计费配置。

## 建议试用流程

1. 在 AstrBot 插件配置里填 `admin_access_token`、`admin_user_id`、`bot_admin_ids`。
2. 管理员私聊机器人执行：

```text
/key 检查
```

3. 普通用户提交：

```text
/key 申请 测试绘图和对话
```

4. 管理员私聊机器人审批：

```text
/key 审核
/key 通过 1 测试用户 1000000
```

5. 用户后续查看：

```text
/key 查看
```

管理员也可以不走申请，私聊机器人主动生成：

```text
/key 生成 123456789 张三 1000000
```

也可以只填写 QQ 和姓名，其它使用默认值：

```text
/key 生成 123456789 张三
```

也支持 key=value 写法，金额可写作 `金额=` 或 `amount=`：

```text
/key 通过 1 名称=测试 金额=100000
```

审批和主动生成不需要传分组或过期天数，分组使用默认配置，有效期固定为 `0`。

修改已有 Key：

```text
/key 修改 123456789 张三 30
/key 修改 123456789 分组 浅夜の梦专属号池
/key 修改名称 ab12cd34 张三
/key 修改分组 ab12cd34 浅夜の梦专属号池
/key 修改金额 ab12cd34 1000000
```

删除已有 Key：

```text
/key 删除 ab12cd34
/key 删除 123456789
```

给已有 Key 增加额度：

```text
/key 加额 ab12cd34 100000
/key 加额 123456789 100000
```
