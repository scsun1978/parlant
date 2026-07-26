# 渠道 BFF（旅客侧，迭代 3-W1）

面向旅客（小程序）的渠道后端：鉴权与租户隔离、匿名续聊会话映射、引擎事件流 → 渠道消息模型转译、渠道层兜底。设计文档：`docs/PRD/A线迭代3-渠道对接方案.md`（§2 职责、§3 接口、§5 流程）。

> **偏离登记**：方案目标形态为 **Spring Boot 3 + WebFlux（Java BFF）**，本实现为 **FastAPI 先行**——本机与 dev03 均无 Java 工具链，接口契约与方案 §3 保持一致，后续 Java 置换时按本契约与测试用例对齐重写（与 admin-bff 同一先例）。

## 目录结构

```
channel/
├── app/
│   ├── main.py        # FastAPI 应用工厂、/health（Parlant/Mongo 双向探测）
│   ├── config.py      # 环境变量（超时预算 preamble 8s / done 45s）
│   ├── auth.py        # 匿名登录（device_id 只存 sha256）、token 校验
│   ├── translate.py   # Parlant 事件 → 渠道消息模型转译（WS/poll/history 共用）
│   ├── deps.py        # 依赖注入（bearer → user_id）
│   └── routes/messages.py  # login / messages / WS stream / poll / history
└── tests/             # pytest 全离线（内存存储 + 假 Parlant + TestClient）
```

复用：`admin.app.store`（Mongo/内存双模，`MONGO_URL` 未配置走内存）、`admin.app.parlant_client.ParlantClient`。

## 环境变量

| 变量 | 缺省 | 说明 |
|---|---|---|
| `PARLANT_BASE_URL` | `http://127.0.0.1:8800` | Parlant REST（compose 注入 `http://parlant-server:8800`） |
| `MONGO_URL` / `MONGO_DB` | 未设置 / `pvg_channel` | 渠道数据（用户/会话/幂等/兜底事件）；未配置走内存 |
| `CHANNEL_PREAMBLE_TIMEOUT_S` | `8` | 同步等 preamble 上限（对齐引擎实测 4.5s） |
| `CHANNEL_DONE_TIMEOUT_S` | `45` | ready/completed 上限（答复 15–40s 预算） |
| `CHANNEL_AGENT_ID` | `xyVHBNLLPg` | 生产 agent |
| `WECHAT_APP_ID` / `WECHAT_APP_SECRET` | 空 | **都配置才启用微信模式，否则匿名模式**（双模判定 `settings.wechat_enabled`） |
| `WECHAT_API_BASE_URL` | `https://api.weixin.qq.com` | 微信 API 地址（预发/代理可换） |
| `WECHAT_GRANT_TYPE` | `authorization_code` | code2session grant_type |
| `WECHAT_TIMEOUT_S` | `10` | 微信 API 超时 |
| `CHANNEL_TOKEN_TTL_S` | `7200` | 渠道 token 有效期（过期 401 `token 已过期，请重新登录`，login 一律重发新 token） |
| `CHANNEL_FALLBACK_TEXT` | 抱歉，系统繁忙，请稍后再试；如需帮助请拨打 021-96990。 | 兜底话术（W2 配置化；接管理后台审批版本为后续项，见遗留） |

启动：`uvicorn channel.app.main:app --port 9100`；测试：`.venv/bin/python -m pytest channel/tests -q`（27 用例）。

## 双模说明（W2）

- **匿名模式**（默认）：`POST /channel/login {device_id}`，用户键 `sha256(device_id)`，单设备续聊
- **微信模式**（配齐 appid/secret）：`POST /channel/wechat/login {code}` → `code2session` 换 openid/unionid → 用户键 `sha256("wechat:" + unionid||openid)`（unionid 优先，同一旅客**跨设备续聊**）；session_key **不落库**（登记遗留：后续用于解密小程序用户信息）
- `GET /channel/config`（公开，无需 token）：`{mode: "anonymous"|"wechat", preamble_timeout_s, done_timeout_s, features: {ws: true, poll: true, fallback: true}}`——**绝不输出 secret**（有测试守护）
- 微信 API 失败（网络/非 200/errcode≠0）→ 502 带微信错误码摘要（不含 secret）；匿名模式调 `/channel/wechat/login` → **501** `{detail: "微信登录未配置，请使用 /channel/login 匿名模式"}`

**微信接入前置条件**（生产）：appid/secret 来自小程序管理后台（secret 仅服务端持有，绝不进小程序包）；BFF 域名需配置进小程序 `request`/`socket` 合法域名且生产必须 HTTPS；预发可用 `WECHAT_API_BASE_URL` 指向代理。

## 接口契约（与方案 §3.1 对齐）

| 方案 §3.1 | 本实现 | 对齐说明 |
|---|---|---|
| `POST /channel/wechat/login` `{code}` → `{channelToken, openid 摘要, resumeSessionId?}` | `POST /channel/wechat/login` `{code}` → `{channelToken, user_id, resume_session_id?}`（微信模式）；`POST /channel/login` `{device_id}`（匿名模式，不受配置影响恒可用） | **W2 已接 code2session**：unionid 优先做用户键（跨设备续聊）；session_key 不落库；未配置时 501 可操作提示 |
| `POST /channel/messages` `{text}` → `{sessionId, preamble{text, displayAs:"placeholder"}}` | `POST /channel/messages` `{text, client_msg_id}` → `{session_id, preamble{text, display_as}, ws_url, status}` | 同步只覆盖 preamble（≤8s，超时 preamble 为空不阻塞）；**幂等** `client_msg_id+user_id` 5 分钟窗口去重，重发返回首次缓存 `status:"duplicate"`（方案 §6） |
| `WS /channel/messages/stream`：`tool_start` / `message_append` / `message_done` / `fallback` / `handoff_offered` | `WS /channel/messages/stream?token=&session_id=`：前四类已实现；`handoff_offered` 属 W3 转人工 | tool→`tool_start("正在为您查询…")`；ai message→`message_append`（preamble 启发式 `display_as`）；ready+stage=completed→`message_done`；引擎 error / 45s 无完成 / 上游断 → `fallback`（兜底文案常量，写 `fallback_events`） |
| WS 不可达降级 `GET /channel/messages/poll?afterOffset=` | `GET /channel/messages/poll?session_id=&after_offset=` → `{events[同 WS 格式], next_offset, done}` | 同转译函数的增量形态 |
| `GET /channel/history` | `GET /channel/history?session_id=&limit=` → `[{role:user/assistant/system, text, ts, display_as}]` | tool/status 不直接暴露（error 折 system 行；human_agent 标 `display_as:"human"`） |
| 会话归属 | 所有会话级接口校验 session 属 token 用户（越权 403；WS 关闭码 4401/4403） | 方案缺口 1「鉴权与租户隔离」 |

超时预算（方案 §6）：preamble 8s / 完成 45s；工具 30s 预算未单独实现（统一并入 45s 完成预算，登记遗留）。

## 数据模型（方案 §4，Mongo 同集群不同库 `pvg_channel`）

- `channel_users`：仅存 `hash(device_id)` + 创建时间（PII 最小化，方案 §9 已决匿名续聊）
- `channel_sessions`：user_id ↔ parlant_session_id 映射、status、last_active（24h 窗口续聊）
- `channel_tokens`：dev 级随机串（生产置换 JWT 2h——方案 §2.1，登记遗留）
- `channel_messages`：幂等缓存（user_id+client_msg_id → 首次响应，5 分钟窗口）
- `fallback_events`：{session_id, trigger(engine_error/timeout/upstream_down), text, ts}（兜底观测，目标 <2%）

## 灰度 / 生产注意点

- **粘性路由**：单副本期会话亲和无要求；多副本需 conversationId 一致性哈希（方案 §6）+ WS 事件经 Redis pub/sub 路由到持有连接的副本（与 admin-bff SSE 同一遗留）
- **JWT 置换**：dev 随机 token → JWT 2h（含 openid 摘要），校验逻辑集中在 `auth.py` 单点替换
- **兜底文案治理**：W1 为代码常量（'抱歉，系统繁忙，请稍后再试；如需帮助请拨打 021-96990。'）；方案 §9 已决走管理后台话术中心审批流，W2 接生效版本（BFF 只读）
- **灰度切流**：openid 哈希 5%→30%→100%（方案 §5.3），本包未含灰度开关（W4）
- **内容审核**：FR-704 前置过滤未实现（W2，ADR-0005 自建审核服务）
- **转人工**：`handoff_offered` 与坐席侧 `/handoff/*` 为 W3 范围，本切片不含
- **Java 置换**：契约以此 README 表格与 `channel/tests/` 为准；置换后本服务退役

## 遗留（W2 剩余 / W3+）

1. ~~微信 `code2session` 换 openid~~ **W2 已实现**；遗留：`session_key` 解密小程序用户信息（手机号/头像，需旅客授权后接入）
2. ~~JWT 渠道 token~~ 部分实现：token TTL 已生效（`CHANNEL_TOKEN_TTL_S`，过期 401 重登）；签名 JWT（可跨副本校验、免查库）为后续项
3. 兜底话术接管理后台审批生效版本（W2 已配置化 `CHANNEL_FALLBACK_TEXT`，接 admin-bff 话术中心为后续项）；内容审核前置（ADR-0005）
4. 转人工工单与坐席桥接（W3）；`handoff_offered` 事件
5. 工具事件 30s 独立超时预算；多渠道（APP/网页）扩展
6. 多副本粘性路由 + Redis pub/sub 事件转发
7. 灰度切流开关（openid 哈希 5%→30%→100%，方案 §5.3，W4）

## 内容审核前置层（W2，ADR-0005）

管线：频控（20 条/分/用户，滑动窗口）→ 注入模式库（角色覆盖/指令忽略/提示窃取/越权诱导/红线诱导 5 类种子）→ 不当内容词表。命中即拦截（不转发引擎），返回 `{"blocked": true, "reply", "rule_category"}` 并写 `moderation_events`（不存原文，text_hash 前 16 位）。

- 规则治理（staff，header `X-Admin-Token`，env `CHANNEL_ADMIN_TOKEN`）：
  `GET/POST /channel/moderation/rules`、`PATCH /channel/moderation/rules/{id}`（enabled 切换，hit_count 自动累计）
- 审计查询：`GET /channel/moderation/events?limit=`
- 拦截话术：`CHANNEL_MODERATION_BLOCK_TEXT` 可配
- 兜底文案治理：`GET/PUT /channel/moderation/fallback-text`（channel_fallback 单文档，版本自增；读取缓存 60s，集合空回退 `CHANNEL_FALLBACK_TEXT`）
- 验收口径（ADR-0005）：注入种子 5 类正反例测试覆盖；LLM 后置抽检、词表运营扩充、多副本频控 Redis、规则审批流整合为后续项
