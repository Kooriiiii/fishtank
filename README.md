# 鱼缸 · FishTank

> 给你的大肥鱼一套能真正跑起来的「人格 + 模型 + 平台」控制台：写人格、连 API、绑定 QQ 和企业微信，然后在聊天软件里直接跟它说话。


1. **创建机器人** —— 一套人格 + 一个模型服务 + 若干平台绑定；
2. **绑定平台** —— QQ 官方机器人、企业微信智能机器人（都是「应用凭证 + WebSocket 长连接」，不需要公网 IP）；
3. **改人格** —— 自由文本提示词 + 结构化人设字段，每次保存留版本快照，可一键回滚；
4. **接 API 聊天** —— 任意 OpenAI 兼容接口（DeepSeek / OpenAI / 硅基流动 / Moonshot / Ollama / one-api），带多轮上下文与长期记忆。

A/B 对比、限流、去重、用量统计这些「演示向」的能力仍然留在后端，但界面只保留运营真正会用的四块。

另有一个 **运行概览** 页：运行时间、平台实例数、今日模型调用与 Tokens、进程 CPU/内存、消息趋势、平台消息排名、模型用量排名、Token 总量（含平均响应、TPM、成功率）、会话用量 Top 10、最近对话。


---

## 快速开始

```bash
cd fishconsole
pip install -r requirements.txt
python run.py                      # → http://127.0.0.1:8000
```

前端依赖（Alpine.js，MIT）已放在 `web/vendor/`，不依赖 CDN。

---

## 5 分钟接上真机

### 第 1 步：建一个模型服务

「模型服务」→ 右侧表单，从预设里挑一个（如 DeepSeek），填 API Key 和模型名，保存后点「测试连接」。

- 第一个创建的服务自动成为全局默认；
- Key 加密落库，界面只显示 `sk-ab **** **** 9xYz`；
- 改配置时 Key 留空 = 沿用原值，不会把脱敏串写进库；
- 兼容任何 OpenAI 格式接口，本地 Ollama 填 `http://127.0.0.1:11434/v1`。

### 第 2 步：建机器人、写人格

「机器人」→「新建机器人」，填名字和一句话设定（头像由名字首字自动生成），然后在「人格」页写系统提示词：

```
你是阿泽养在鱼缸里的一条咸鱼，叫咸鱼酱。
说话懒懒的，爱用叠词，三句不离躺平，但会认真记住阿泽说过的事。
```

人格留空时，改用下方的结构化字段（性格标签 / 背景故事 / 说话风格 / 口头禅 / 禁忌话题）拼装提示词。

无论哪条路径，系统都会在结尾追加两块硬约束：**只用列出的记忆、不许编造共同经历**。

### 第 3 步：绑定 QQ 官方机器人

1. 手机 QQ 扫码登录 [q.qq.com](https://q.qq.com)，创建机器人，拿到 **AppID / AppSecret**；
2. 在「功能配置」里开通群聊能力（只私聊可以不开）；
3. 回到控制台 → 机器人 → 平台 → QQ 官方机器人 →「填写凭证并绑定」→「连接」；
4. 状态变「在线」后，去 QQ 里私聊机器人，或在群里 @ 它。

### 第 4 步：绑定企业微信智能机器人

1. 企业微信管理后台 → 应用管理 → 智能机器人 → API 模式 → **使用长连接**；
2. 拿 **BotID / Secret**，把机器人加进需要它的会话；
3. 控制台里绑定 → 连接；
4. 企业微信里给它发消息。

### 连不上 / 不回消息怎么办

点「运行日志」，里面是唯一的真相来源：订阅失败的 errcode、消息收发、发送失败原因都在。常见情况：

| 现象 | 原因 |
|---|---|
| QQ 一直「连接中」，日志里有 `[botpy] 获取token失败` | AppID / AppSecret 填错，或机器人没发布 |
| QQ 能连上但群聊不回 | 群聊权限没开通，或没有 @ 机器人 |
| QQ 日志里「收到私聊消息」但接着是「重复事件已忽略」 | 同一个 AppID 有两条长连接，另一条先处理过了；重新点一次「连接」会断开旧连接 |
| QQ 私聊不回，日志里没有入站消息 | 机器人没发布 / 沙箱开关与凭证环境不匹配 |
| 企微日志里 `errcode=40001` | BotID 与 Secret 不属于同一个智能机器人 |
| 企微日志里 `errcode=6000` | 同一条消息的应答冲突，适配器会自动退避重试 |
| 企微收到消息但回复超时 | 模型太慢；占位消息已经在跑，检查模型服务是否可用 |

---

## 关键机制

### 运行概览 / 统计

数据源只有 `conversation` 一张表：每次对话都会落库人设版本、渠道、模型名、token、耗时与状态，统计页只是按不同维度聚合，**不会出现「统计和实际对话对不上」**。

- **时间范围**：1 天 / 3 天 / 7 天 / 30 天 / 全部（对齐 AstrBot 的 1 天 / 3 天 / 1 周，再补两档）；
- **时间分桶按本地时区**：库里存 UTC，但「今天」按你手机上的今天算，东八区不会整体偏 8 小时；
- **成功率需要分母**：调用失败也会写一条 `status=error` 的对话记录（不带回复），所以成功率是真实的，而不是永远 100%；
- **失败不算用量**：`/api/usage` 与多轮上下文都只认 `status=done`，失败记录不会污染统计和上下文；
- **CPU / 内存**：装了 `psutil` 就显示，没装自动隐藏这两张卡（不是硬依赖）；
- 概览页每 10 秒自动刷新一次。

### 多轮上下文

每个机器人可以单独设置「上下文轮数」（0–50，默认 8）。上下文按 `session_key`（平台 + 会话 + 绑定实例）隔离，**同一个机器人在不同人私聊、不同群里聊天不会串台**。

### 模型路由

`角色绑定 > 全局默认 > 离线 Mock`。切换模型不需要重启，也不影响其他机器人。

### 人格版本

每次保存都生成一个新版本，记录完整快照。回滚 = 以目标版本内容生成新版本并激活，历史永不改写，所以回滚本身也能再回滚。

### 记忆

记忆条目化，可增删改置顶。检索规则刻意可解释：置顶必中，其余按问题关键词命中，最多 6 条；命中了哪几条会显示在「调试」右栏。删掉一条记忆再问同样的问题，机器人就真的不记得了。

### 平台时序

- **企微**：先发 `finish=false` 的流式占位，算完再发 `finish=true` 全量替换，两次必须用同一个 `stream.id`、并复用回调帧里的 `req_id`；
- **QQ**：被动回复必须带 `msg_id`（刚收到那条消息的 id）和递增的 `msg_seq`，否则会被判成「主动消息」而拒绝——群聊主动消息默认没有权限；
- **去重**：平台会重复推送，用 `event_id / msgid / 消息 id` 做幂等键，先落库再处理。

### 渠道连接状态

点「连接」后按钮会变成「连接中…」，后端会**等适配器真正连上（或失败）再返回**——QQ 要等网关握手 `on_ready`，企微要等订阅成功，所以这里最多等 12 秒。

- 平台页每 3 秒轮询一次 `/api/channels/runtime`，所以适配器掉线、或超过 12 秒才连上的情况，状态也会自己刷新；
- 状态以内存里的适配器为准，库里只做持久化；
- 上次停在「连接中」的绑定，重启后也会自动重连；
- 概览页的「平台实例数」= 已运行 / 已绑定，连上之后立刻能看到 1/1。

**同一个 AppID 只保留一条连接**：QQ 会把事件广播给同账号的所有长连接，两条连接拿到同一条消息后会互相把它判成「重复事件」，结果谁都不回。所以连接新绑定时会先断开同 AppID 的旧连接；解绑 / 删角色也会停掉连接。

**不留僵尸连接**：botpy 的断线重连任务与等待方是分开的，只取消等待方 socket 还活着。适配器停机时会显式取消这些连接任务；万一仍有残留，它传来的消息会被直接丢弃（不进管线、不消耗幂等键），避免「消息被吃掉但没人回」。

---

## 项目结构

```
fishconsole/
├── run.py                      一键启动
├── requirements.txt
├── app/
│   ├── main.py                 FastAPI 入口与全部路由
│   ├── db.py                   SQLite 建表 + 老库补列迁移
│   ├── security.py             凭证加密 / 脱敏（按字段名判断敏感度）
│   ├── seed.py / seed_data.json  3 个示例角色（带版本号，升级时自动替换）
│   ├── adapters/
│   │   ├── base.py             适配器基类：状态机、错误簿记、日志环形缓冲
│   │   ├── registry.py         平台配置 schema 声明
│   │   ├── manager.py          事件总线（管线跑在线程池，不阻塞事件循环）
│   │   ├── mock.py             本地全链路模拟
│   │   ├── qq_official.py      QQ 官方机器人（botpy 长连接）
│   │   ├── wecom.py            企业微信智能机器人（长连接 + 流式占位）
│   │   └── feishu.py / personal_wechat.py   骨架 / 合规置灰
│   └── services/
│       ├── providers.py        模型服务 CRUD + 加密 + 连通性自检
│       ├── llm.py              Prompt 组装 + 多轮 messages + Mock / OpenAI 兼容
│       ├── pipeline.py         去重 → 限流 → 组装 → 调模型 → 落库（失败也落痕）
│       ├── persona.py          人设版本化（含人格自由文本）
│       ├── memory.py           记忆条目与检索注入
│       ├── stats.py            运行概览 / 区间统计（趋势、排名、Token、成功率）
│       ├── catalog.py          角色 / 渠道绑定 / 用量
│       ├── dedup.py / ratelimit.py / settings.py
├── web/index.html              单文件 SPA（Alpine + 手写主题层，无构建）
├── web/vendor/alpine.min.js    Alpine.js（MIT），本地化后断网也能用
├── web/favicon.svg
├── tests/                      90 项回归测试
└── scripts/fix_dup_seed.py     维护脚本：清理重复的示例角色
```

---

## API 一览

```
# 机器人
GET    /api/characters
POST   /api/characters
GET    /api/characters/{id}              含人设版本 / 记忆 / 绑定 / 模型路由
PUT    /api/characters/{id}
DELETE /api/characters/{id}              会先停掉它的渠道连接
PUT    /api/characters/{id}/model        绑模型服务 + 上下文轮数

# 人格
GET    /api/characters/{id}/versions
POST   /api/characters/{id}/versions     每次保存生成新版本
GET    /api/characters/{id}/versions/diff
POST   /api/versions/{id}/rollback

# 记忆
GET/POST /api/characters/{id}/memories
PUT/DELETE /api/memories/{id}

# 平台
GET    /api/platforms/schema             渠道目录 + 每个平台自己声明的字段
GET    /api/channels/runtime             所有绑定的真实连接状态（前端轮询用）
POST   /api/characters/{id}/channels     按 schema 绑定
PUT    /api/channels/{id}                改凭证（脱敏串不覆盖真实密钥）
POST   /api/channels/{id}/connect|disconnect
GET    /api/channels/{id}/logs           运行时日志（联调就靠它）
POST   /api/channels/{id}/simulate       Mock 全链路注入

# 模型服务
GET    /api/providers/presets            常见服务商预设
GET/POST /api/providers
PUT/DELETE /api/providers/{id}
POST   /api/providers/{id}/default
GET    /api/providers/{id}/models        拉服务端模型列表
POST   /api/providers/{id}/test          连通性自检

# 对话与运行
POST   /api/chat
GET    /api/stats/overview               概览 KPI：运行时间 / 平台数 / 今日调用 / CPU 内存
GET    /api/stats?range=1d|3d|7d|30d|all 区间统计：趋势 / 平台排名 / 模型排名 / 会话 Top10
GET    /api/usage                        旧的按角色用量（保留兼容）
GET/PUT /api/settings, /api/settings/rate_limit, /api/settings/dedup
```

---

## 已知限制

1. **真机通道只用个人凭证做过有限验证。** QQ 与企业微信的协议实现依据官方文档与 AstrBot 的公开实现思路，企微长连接用本地假服务端做了端到端测试（含订阅、占位、替换、errcode）；QQ 适配器的 `msg_id / msg_seq` 被动回复、消息清洗、连接回收有单元测试覆盖。不同主体的权限差异仍需自行验证，失败原因会写进运行日志。
2. **QQ 官方通道不支持主动消息**，也不能发 At / 语音 / 视频 / 引用；Markdown 需要单独申请（控制台里默认关闭）。
3. **无鉴权**：定位单机单用户，**不要直接暴露到公网**。
4. **费用是估算值**，按固定单价推算，仅供观察。

---

## 合规与技术来源

- 只接官方开放通道：QQ 官方机器人、企业微信智能机器人
- 凭证加密存储、界面脱敏、不写日志；记忆逐条可查可改可删。
