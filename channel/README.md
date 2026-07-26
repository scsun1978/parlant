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

启动：`uvicorn channel.app.main:app --port 9100`；测试：`.venv/bin/python -m pytest channel/tests -q`（16 用例）。

## 接口契约（与方案 §3.1 对齐）

| 方案 §3.1 | 本实现 | 对齐说明 |
|---|---|---|
| `POST /channel/wechat/login` `{code}` → `{channelToken, openid 摘要, resumeSessionId?}` | `POST /channel/login` `{device_id}` → `{channelToken, user_id, resume_session_id?}` | **W1 匿名续聊**（方案 §9 已决）：openid 维度的 device_id 只存 sha256；微信 code2session 换 openid 为 W2+ 接入点，契约字段不变 |
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

## 遗留（W2+）

1. 微信 `code2session` 换 openid（W1 用 device_id 匿名，契约字段已预留）
2. JWT 渠道 token（2h）置换随机串
3. 兜底话术接管理后台审批生效版本；内容审核前置（ADR-0005）
4. 转人工工单与坐席桥接（W3）；`handoff_offered` 事件
5. 工具事件 30s 独立超时预算；多渠道（APP/网页）扩展
6. 多副本粘性路由 + Redis pub/sub 事件转发
