# HCM 下单通知配置指引（钉钉 / 企业微信）

> 适用版本：HCM v2（hcm-dispatcher 事件钩子 + hcm-web 面板）。
> 功能：每一次新订单成交后，自动推送一条消息到 **钉钉群** 和/或 **企业微信群**（可经微信插件转发到个人微信）。
> 推送内容核心三要素：**🔔 有新订单啦 / 进单价格 / 手数**。

---

## 一、工作原理（先读懂，再配置）

```
新订单进入 PLACED（mt5_ticket 已分配）
        │
        ▼
hcm-dispatcher · OrderTracker
        │  asyncio.create_task(notifier.on_order(snap))   ← 非阻塞，绝不拖慢下单
        ▼
Notifier（dispatcher/dispatcher/notifier.py）
        │  读取 PG hcm_config.metadata 中的 notification.* 配置（60s 缓存）
        │  · enable_trade_alert = true 才推送
        │  · 钉钉：对 timestamp+secret 做 HMAC-SHA256 加签后 POST
        │  · 企业微信：直接 POST markdown
        ▼
   钉钉群机器人  /  企业微信群机器人（→ 个人微信）
```

关键特性（设计约束，无需你干预）：
- **配置驱动、零硬编码**：所有 webhook / secret / 开关都存于 PG `hcm_config.metadata`，面板只读写这张表。
- **非阻塞**：通知在独立 `asyncio` 任务里发送，3 秒超时、失败重试 2 次，任何网络抖动都不会影响订单流转。
- **钉钉加签**：自动加签，你只要把「加签」安全设置里的 secret 贴进面板即可。
- **红涨绿跌（中国习惯）**：BUY 显示 🔴 买入，SELL 显示 🟢 卖出。

---

## 二、前置条件

1. 已部署 hcm-dispatcher / hcm-web（本次改动需重新构建/重启后生效，**部署前通知不会发出**）。
2. 你本人是钉钉群 / 企业微信群的群主或管理员（才能添加机器人）。
3. dispatcher 容器能访问外网（`oapi.dingtalk.com` / `qyapi.weixin.qq.com`）。
4. 面板路径：**左侧菜单「通知」** → `/system/notifications`。

---

## 三、钉钉机器人配置

1. 打开目标钉钉群 → 右上角 **···** → **群设置** → **机器人** → **添加机器人** → **自定义**（通过 Webhook 接入）。
2. 安全设置 **务必选「加签」**（不要选「自定义关键词」或「IP 地址」），点「下一步」。
3. 复制两样东西并保存好：
   - **Webhook 地址**：形如 `https://oapi.dingtalk.com/robot/send?access_token=xxxx`
   - **加签密钥 Secret**：形如 `SECxxxxxxxxxxxxxxxx`
4. 完成添加，机器人出现在群里。

> 为什么用「加签」？加签把 secret 放在服务端签名，URL 即使泄露也无法被他人冒用；本系统已内置加签逻辑，你只需填 secret。

---

## 四、企业微信机器人配置（转发个人微信）

1. 打开目标企业微信群 → 右上角 **···** → **添加群机器人** → **新建一个机器人**。
2. 复制 **Webhook 地址**：形如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx`。
3. （可选）**转发到个人微信**：在企业微信里开启「微信插件」（设置 → 微信插件 → 关注），群消息即可推送到你的个人微信。
4. 企业微信机器人一般**无需密钥**，面板里「密钥（可选）」留空即可。

> 说明：企业微信官方**不支持**直接把消息发到个人微信（非企业成员）。「微信群机器人 → 个人微信」是经企业微信「微信插件」间接转发，仅对**已加入对应企业的成员**生效。若你本人不在该企业微信里，则只能在企业微信内收到。

---

## 五、在 HCM 面板填写

进入 **通知** 页面，按分组填写：

### 1. 钉钉 (DingTalk)
| 字段 | 填写内容 |
| --- | --- |
| Webhook 地址 | 第三节复制的 `https://oapi.dingtalk.com/robot/send?access_token=...` |
| 加签密钥 (Secret) | 第三节复制的 `SEC...` |

### 2. 企业微信 (WeCom)
| 字段 | 填写内容 |
| --- | --- |
| Webhook 地址 | 第四节复制的 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...` |
| 密钥 (可选) | 一般留空 |

### 3. 通知开关
| 开关 | 说明 | 默认 |
| --- | --- | --- |
| **交易通知 (新订单)** | **每笔新订单推送：有新订单啦 / 进单价格 / 手数** | 开 |
| 信号通知 | 信号触发类通知（预留） | 开 |
| 风险通知 | 风险类通知（预留） | 开 |
| 系统通知 | 系统事件通知（预留） | 关 |

> **要点**：要收到下单推送，必须保证 **「交易通知 (新订单)」为开启**，且至少一个通道的 Webhook 地址已填。

填好后点 **保存**。

### 关于 secret 的安全显示（重要）
- 保存后再次打开本页，`加签密钥` / `密钥` 会显示为 `***xyz`（脱敏）。
- **这是正常保护，不是没保存成功。** 只要你没改这一栏，直接点保存也不会覆盖真密钥——系统会识别 `***` 脱敏串并保留原值。
- 想**更换**密钥：清空该栏、填入新值再保存即可。

---

## 六、消息格式示例

钉钉 / 企业微信收到的 markdown 示例：

```
### 🔔 有新订单啦
> **账户**: 9
> **品种**: XAUUSD
> **方向**: 🔴 买入 BUY
> **进单价格**: 2398.50
> **手数**: 0.5
> ticket: 293852200
```

- **进单价格**：优先取实际成交价 `filled_price`，回退到信号入场价 `entry_price`。
- **手数**：该笔订单的下单手数 `lot`。
- **方向**：BUY 红 / SELL 绿（中国习惯）。

---

## 七、验证是否生效

1. 配置保存后，等待下一笔真实订单（或测试信号）成交。
2. 钉钉群 / 企业微信群应收到上面格式的消息。
3. 也可在 dispatcher 容器日志里观察：
   - 成功：`Notifier push ok → https://oapi.dingtalk.com/...`
   - 失败（不影响下单）：`Notifier push ... failed` / `HTTPError ...`（多为 secret 不匹配或网络不通）。
4. 若**完全没动静**：先确认「交易通知」开关已开、Webhook 地址无误、secret 与机器人安全设置一致。

---

## 八、常见问题排查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 完全收不到消息 | 「交易通知」开关关着 | 打开「交易通知 (新订单)」 |
| 钉钉报 `sign not match` / `不合法` | secret 填错或与机器人安全设置不一致 | 重新核对「加签密钥」 |
| 钉钉报 `robot not found` / `invalid token` | Webhook 地址复制不全或机器人被删 | 重新从机器人设置复制完整 URL |
| 企业微信收不到 | Webhook 地址错误 / 群机器人被移除 | 重新复制企业微信机器人 Webhook |
| 个人微信收不到 | 未开启「微信插件」或不在该企业微信 | 开启微信插件；企业微信机器人只能推给本企业成员 |
| 偶尔漏发 | 钉钉限频（约 20 条/分钟）或网络抖动 | 系统设计已含重试；高频下单时注意限频 |
| secret 显示 `***` | 这是脱敏，不是故障 | 无需处理；改动才需重填 |

---

## 九、配置键参考（后台 / 运维视角）

所有配置存于 PostgreSQL `hcm_config` 库的 `metadata` 表（`config_key` 形如 `notification.*`），由面板通过 `ConfigProviderV3` 读写：

| config_key | 含义 | 默认值 |
| --- | --- | --- |
| `notification.dingtalk_webhook` | 钉钉 Webhook 地址 | 空 |
| `notification.dingtalk_secret` | 钉钉加签密钥（加密展示） | 空 |
| `notification.wecom_webhook` | 企业微信 Webhook 地址 | 空 |
| `notification.wecom_secret` | 企业微信密钥（可选） | 空 |
| `notification.enable_trade_alert` | 交易/下单通知开关 | `true` |
| `notification.enable_signal_alert` | 信号通知开关 | `true` |
| `notification.enable_risk_alert` | 风险通知开关 | `true` |
| `notification.enable_system_alert` | 系统通知开关 | `false` |

> 直接改 PG 属于写操作，需走变更流程；日常请用面板。**secret 写入有保护**：空值或 `***` 脱敏串都不会覆盖已有真值。

---

## 十、部署与生效说明（给实施者）

- **前端**：`hcm-web/frontend` 已重写 `Notifications.tsx`（分组表单 + 脱敏回写保护），需重新 `npm run build` 并重建 `hcm-web` 镜像。
- **后端**：`hcm-dispatcher/dispatcher/notifier.py` 为新增发送层；`order_tracker.py` / `stream_consumer.py` / `main.py` 已挂接；`hcm-web/web/api/system.py` 已扩展配置读写与 secret 脱敏/防回写。
- **生效动作**：需把 `notifier.py` 部署进 dispatcher 容器并重启 `hcm-dispatcher`；hcm-web 需重建镜像并重启。重启服务属变更操作，按项目红线需确认后执行。
