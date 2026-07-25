# 浦东机场智能客服 · 运营后台 BFF（切片 1-4，对齐 v3.0）

管理后台的后端（Backend-for-Frontend）：对上前台（定制页面），对下调 Parlant REST + RAG 服务 + MongoDB，在其上自建**权限、审批流、审计留痕、会话中心、召回测试台、试聊沙盒、话术预览、质检工作台**（设计文档：`docs/PRD/机场智能客服-管理后台设计方案.md` **v3.0**——质检闭环为一期主干，编辑器群降级为工作流下游工具；对应 §4.1 质检闭环、§4.3 现场与审计、§5 编辑器群）。

## 目录结构

```
admin/
├── app/
│   ├── main.py            # FastAPI 应用工厂与组装、/health 三向探测、/app 静态托管
│   ├── config.py          # 环境变量配置（含 PROD_AGENT_ID）
│   ├── auth.py            # dev 级令牌认证 + 五角色 RBAC（SSE 端点支持 ?token=）
│   ├── store.py           # 治理数据存储抽象（Mongo / 内存降级）
│   ├── audit.py           # 审计日志（append-only）
│   ├── deps.py            # 路由依赖注入入口
│   ├── parlant_client.py  # Parlant REST / RAG 服务同步客户端（含 sessions/events/agents）
│   └── routes/            # auth / guidelines / canned(+preview) / approvals / terms / journeys
│                          # / rag / audit / sessions / sandbox / bad_cases
├── web/                   # 定制前端（vanilla JS + 原生 SSE，零构建，无外部 CDN，内网可开）
│   ├── workbench.html     # 质检工作台（默认页 · 一期主干：向导四步/气泡回放/归因卡片/修复向导）
│   ├── index.html         # 会话中心（三栏：列表/对话/trace）
│   ├── knowledge.html     # 知识库（文档浏览/缺口看板/补录草稿 三标签页）
│   ├── rag.html           # 召回测试台（BM25/向量/RRF 三阶段并列）
│   ├── sandbox.html       # 试聊沙盒（草稿双栏 + 聊天窗，iframe 可嵌入）
│   ├── canned-preview.html# 话术预览（jinja2 实时渲染，iframe 可嵌入）
│   ├── app.js             # 会话中心逻辑
│   └── styles.css         # 共享样式（含顶部互链 nav，workbench 排首位）
├── tests/                 # pytest，全离线（内存存储 + 假上游 + ASGI 客户端）
└── requirements.txt
```

## 环境变量

| 变量 | 缺省 | 说明 |
|---|---|---|
| `MONGO_URL` | 未设置 | 治理数据 Mongo 地址（容器内 `mongodb://mongo:27017`）；未设置或连接失败时降级进程内内存存储 |
| `MONGO_DB` | `pvg_admin` | 治理数据库名（与 Parlant 会话库同集群不同库，ADR-0004） |
| `PARLANT_BASE_URL` | `http://127.0.0.1:8800` | Parlant REST 地址 |
| `RAG_URL` | `http://127.0.0.1:8901` | RAG 检索服务地址 |
| `PROD_AGENT_ID` | `xyVHBNLLPg` | 生产 agent id（试聊沙盒"生产快照"的来源） |
| `BUNDLES_DIR` | `../knowledge/bundles/v2-miniprogram-20260719b` | 知识补录 bundle 写入目录（容器内 `/data/knowledge/bundles/v2-miniprogram-20260719b`） |
| `ADMIN_USERS_JSON` | 内置 admin/admin123=admin | 用户表 JSON 数组 `[{"username","password","role"}]`，缺省打警告日志（仅开发期） |

`pymongo` 为可选依赖（惰性 import）：仅启用 Mongo 存储时需要，测试与本地开发不依赖。jinja2 复用 FastAPI 自带依赖（话术预览，惰性 import）。

## 启动与测试

```bash
uvicorn admin.app.main:app --port 9000     # 启动（仓库根目录，.venv 已含 fastapi/uvicorn）
.venv/bin/python -m pytest admin/tests -q  # BFF 测试（96 用例，全离线）
.venv/bin/python -m pytest scripts/test_pvg_rag_service.py scripts/test_pvg_rag_reindex.py -q  # RAG 服务测试（13 用例）
```

启动后打开 `http://127.0.0.1:9000/app/`（或 `/app/sessions.html` 同义于 index.html 所在目录）即会话中心；顶部 nav 互链四页：会话中心 `/app/`、召回测试台 `/app/rag.html`、试聊沙盒 `/app/sandbox.html`、话术预览 `/app/canned-preview.html`（后两页为 iframe 可嵌入组件形态）。

## 权限矩阵（五角色，dev 级）

| 能力 | operator 运营 | reviewer 审核 | auditor 审计 | supervisor 班长 | admin 管理员 |
|---|---|---|---|---|---|
| 登录 / 读列表（guideline/canned/terms/journeys） | ✅ | ✅ | ✅ | ✅ | ✅ |
| guideline / canned 起草与修改（进审批单） | ✅ | — | — | — | ✅ |
| 审批队列查看 / approve / reject | — | ✅ | 只读 | — | ✅ |
| terms 直建（低合规，无需双人） | ✅ | — | — | — | ✅ |
| RAG /stages 透传 | ✅ | ✅ | ✅ | ✅ | ✅ |
| RAG /reload 热加载 | — | — | — | — | ✅ |
| 审计日志查询 / 导出 | — | — | ✅ | — | ✅ |
| 会话监控 / 事件回放 / SSE 流 | — | — | ✅ | ✅ | ✅ |
| 会话接管/恢复（takeover）、人工代发 | — | — | — | ✅ | ✅ |
| 试聊沙盒 start/chat/stop | ✅ | ✅ | — | — | ✅ |
| 话术渲染预览 /api/canned/preview | ✅ | ✅ | — | — | ✅ |
| bad case 队列/详情/告警（只读） | ✅ | ✅ | ✅ | — | ✅ |
| bad case intake/归因/挂接 | ✅ | ✅ | — | — | ✅ |
| bad case 沙盒重放 verify / 关闭 close | — | ✅ | — | — | ✅ |

硬规则（§2）：
- **双人复核**：approve/reject 要求 `审批人 ≠ 起草人`（即使 admin 也不能批自己的单）；
- **操作全留痕**：所有写操作（含 approve/reject/takeover/代发/login 失败）追加 `audit_logs`（actor/role/action/target/before/after/ts），append-only，不提供任何更新/删除接口；
- 令牌为进程内随机串（dev 级），重启失效。

## 切片 1+2 API 面

- `GET /health` → `{status, parlant, rag, mongo}` 三向探测（上游挂时 `status=degraded`，内存降级时 `mongo=memory`）
- `POST /api/auth/login` → `{token, role}`
- Guideline：`GET /api/guidelines`（透传）、`POST /api/guidelines/draft`、`PATCH /api/guidelines/{gid}`（先取原值供 diff，经审批流生效）
- 审批流（guideline/canned 共用）：`GET /api/approvals?status=pending_review`、`GET /api/approvals/{id}`（含 before/payload）、`POST /api/approvals/{id}/approve`（BFF 调 Parlant 落实体并记录 upstream_id）、`POST /api/approvals/{id}/reject`（附理由，不触上游）
- Canned：`GET /api/canned-responses`（透传）、`POST /api/canned-responses/draft`
- Terms：`GET /api/terms`（透传）、`POST /api/terms`（operator 直建 + 审计）
- Journeys：`GET /api/journeys`（透传）
- RAG：`POST /api/rag/stages`（透传）、`POST /api/rag/reload`（admin）、`GET /api/rag/health`
- 审计：`GET /api/audit-logs`（auditor/admin，actor/action/时间区间过滤 + 分页）
- **会话中心（§4.7，切片 2）**：
  - `GET /api/sessions`：透传列表 + 工单视图字段（`last_message` 摘要、`last_activity_utc`、`unanswered` 疑似已读不回=末条 message 为 customer）
  - `GET /api/sessions/{sid}/events?min_offset&limit`：分页透传（`next_offset` 翻页），trace 回放数据源
  - `PATCH /api/sessions/{sid}/takeover {mode: manual|auto}`（supervisor/admin）：接管=manual、恢复=auto，写审计
  - `POST /api/sessions/{sid}/messages {message}`（supervisor/admin）：以 `source=human_agent` 代发，`participant.display_name` 取登录用户名，写审计
  - `GET /api/sessions/{sid}/stream?min_offset`（SSE）：`event: parlant-event, data: 事件 JSON`；EventSource 无法带请求头，支持 `?token=` 查询参数
- **试聊沙盒（§4.2，切片 3，operator/reviewer/admin）**：
  - `POST /api/sandbox/start {guideline_draft?}`：生产快照+草稿叠加（见下节），返回 `{sandbox_agent_id, copied_guidelines, draft_overlaid, draft_appended}`
  - `POST /api/sandbox/chat {sandbox_agent_id, message}`：建/复用 session 发消息，轮询至 `ready 且 stage=completed` 判定本轮完成，返回 `{reply, messages[{text,preamble}], tools, session_id}`（单轮上限 60s，超时 504）
  - `POST /api/sandbox/stop {sandbox_agent_id}`：删除 test agent；上游无删除接口时降级为逐条停用其规则（返回 `disabled_guidelines`）
- **话术预览（§4.3，切片 3）**：`POST /api/canned/preview {value, sample_fields}` → jinja2 StrictUndefined 渲染，槽位悬空返回 `error`（渲染错误与未定义槽位同口径返回，HTTP 仍 200）
- **知识库管理（v3.0 §5 + §4.1 知识缺失闭环，切片 5）**：
  - 只读代理：`GET /api/knowledge/docs`（q/knowledge_type/valid_state/page/page_size 全透传）、`GET /api/knowledge/docs/{id}`（text 全文）、`GET /api/knowledge/gaps`、`GET /api/knowledge/gaps/stats`
  - 补录草稿流（collection `knowledge_drafts`）：`POST /api/knowledge/drafts`（operator，可从 bad case prefill 带值）、`GET /api/knowledge/drafts?status=`、`POST /api/knowledge/drafts/{id}/approve`（reviewer/admin，双人复核）→ **生成 bundle 文件 → RAG /reindex → published**、`POST /api/knowledge/drafts/{id}/reject`（附理由，不写文件）
  - approve 失败分支：reindex 失败保持 `approved` + `reindex_error` 错误尾，**bundle 文件不丢**；重试由运维直接调 RAG `POST /reindex`（approved 状态不可重复 approve，409，登记于此）
- **质检工作台（v3.0 §4.1，切片 4，一期主干）**：
  - `POST /api/bad-cases/intake`：扫描 mode=manual 会话按 session_id 去重摄入"待复盘"（source=handoff_review，title=首条 customer 消息前 60 字）
  - `GET /api/bad-cases?status=&limit=`：队列（响应含四态 counts）；`GET /api/bad-cases/{id}`：详情 + 会话事件流
  - `POST /{id}/attribute {type,note}`：四选一归因（knowledge/rule/tool/model）+ 生成修复 prefill，pending_review→pending_fix
  - `POST /{id}/link-fix {ref_type,ref_id}`：挂接修复物（knowledge_draft/guideline_draft/tool_ticket/corpus_item），pending_fix→pending_verify
  - `POST /{id}/verify`（reviewer/admin）：复用沙盒核心（快照→chat 首条用户问题→stop）重放，结果存 verify，**不自动判过**
  - `POST /{id}/close`（reviewer/admin）：硬条件=fix_ref + verify 齐全（缺项 409 说明），closed + 用例入 `regression_items` 集合
  - `GET /api/bad-cases/no-reply-alerts`：已读不回告警（可靠性特例，末条 customer 无后续 AI 回复或其后 error 状态），不进归因流
  - 所有状态迁移校验前置状态（不符 409），全部写操作进 audit_logs

上游不可用时写接口统一返回 502。Parlant/RAG 字段契约按已核实 REST 照抄（见 `app/parlant_client.py` docstring）。

## 会话中心（切片 2）

**页面结构**（`admin/web/`，挂载 `/app`）：

- 左栏：会话列表，10s 轮询 `/api/sessions`；显示 title/mode 徽标/最近消息摘要/最近活动时间，疑似"已读不回"红点；点选进入
- 中栏：对话视图，先拉历史事件分页再开 `EventSource` 接 `/api/sessions/{sid}/stream` 实时追加；customer/ai_agent/human_agent 三色气泡，tool 事件折叠块，status 事件折叠为阶段条；preamble 消息标注"占位"（启发式：`data.preamble` 或 `data.metadata.preamble` 为真）；顶部接管条显示当前 mode 与接管/恢复按钮，接管后出现人工输入框
- 右栏：trace 面板，点击一条 AI 回复展开该轮窗口（上一条 customer 消息 → 本轮 ready:completed）：status 阶段序列与耗时、tool 入参出参 JSON、事件 data 中携带的回复来源信息（规则/话术字段，防御式展示）
- 登录：用户名密码换 token，存 localStorage，所有 fetch 带 `Authorization`（SSE 走 `?token=`）

**SSE 通道说明**（v2.1 §4.7 Q6 定稿的单 BFF 直连模式）：

```
Parlant GET /sessions/{id}/events?min_offset&wait_for_data=60（长轮询，504 空返回即重试）
   → BFF `_sse_stream` 逐条转 SSE（event: parlant-event）→ 前端 EventSource
```

客户端断开由 Starlette 取消生成器即停；单次上游长轮询最长 60s，断开检测最多滞后一个轮询周期（dev 级可接受）。**多副本部署时事件需经 Redis pub/sub 转发至正确前端连接——遗留登记（代码 `routes/sessions.py` 注释 + 本文遗留清单）**。

**手工冒烟脚本**（对本机 :8800 真会话验证流式链路）：

```bash
# 1. 启动 BFF（默认 PARLANT_BASE_URL=http://127.0.0.1:8800）
uvicorn admin.app.main:app --port 9000

# 2. 登录拿 token（缺省账号 admin/admin123）
TOKEN=$(curl -s -X POST http://127.0.0.1:9000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# 3. 找一个真实会话（也可先用 Parlant 自带 UI 发一条消息造一个）
curl -s http://127.0.0.1:9000/api/sessions -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | head -30
SID=<上一步的会话 id>

# 4. 观察 SSE 实时流（另开一个终端向该会话发消息，这里会实时出现 parlant-event）
curl -N "http://127.0.0.1:9000/api/sessions/$SID/stream?min_offset=0&token=$TOKEN"

# 5. 或直接用浏览器开 http://127.0.0.1:9000/app/ ，登录后点选会话即可看到流式事件
```

## 召回测试台 / 试聊沙盒 / 话术预览（切片 3）

**召回测试台 `/app/rag.html`**（§4.4，FR-804）：查询输入 + count 选择 → BM25/向量/RRF 三栏并列展示各阶段 top N（rank/score/标题/类型/有效期，点击展开 content 前 600 字——服务端已截断）；三阶段都命中的文档绿色高亮；"无结果"与"低分（低于首名 50%）"警示样式；最近 20 条查询存 localStorage 可点击重跑；admin 角色显示"热加载索引"按钮（调 `/api/rag/reload` 并显示 docs 数）。运营可据此自助判断"答不上来是检索没找到还是生成没用好"。

**试聊沙盒 `/app/sandbox.html`**（§4.2，v2.1 Q5 定稿"生产快照+草稿叠加"）：

- 快照叠加实现（`routes/sandbox.py`）：start 时创建 test agent（`sandbox-<ts>-<user>`），把 `PROD_AGENT_ID`（默认 xyVHBNLLPg）的全部 guideline 逐条复制进新 agent（仅保留 condition/action/criticality 等业务字段，tags 重写为 `agent:<newid>` 归属标签）；携带 `guideline_draft` 时，**同 condition 的生产规则被草稿字段覆盖**，否则草稿作为新规则追加——保证"试的就是要发的"，无漂移窗口
- chat 复用切片 2 的长轮询逻辑：发消息后轮询事件，`ready 且 stage=completed` 判定本轮完成（与评测口径一致：preamble 不算答复，拼接回复中 preamble 逐条标注）；session 在 BFF 进程内按沙盒 agent 复用
- stop 删除 test agent（`DELETE /agents/{id}`）；**若上游无删除接口则降级为逐条 PATCH `enabled=False` 停用其全部规则（替代方案，登记于此）**；start/stop 均写审计
- 页面：左侧草稿规则"当/则"双栏输入 + 开始沙盒；右侧聊天窗（回复 + tools 折叠块，preamble 灰色"占位"标注）；结束按钮销毁；28 分钟横幅提醒、30 分钟前端自动 stop

**话术预览 `/app/canned-preview.html`**（§4.3）：模板编辑框（plain textarea）+ 字段样例 JSON + 400ms 防抖实时渲染；常见槽位提示 chips（`{{knowledge_answer}}` `{{knowledge_source}}` `{{flight_info}}`，点击插入）；服务端 jinja2 `StrictUndefined` 渲染——槽位悬空即报错（槽位治理的第一道防线，防线上模板不可选）。

三页与切片 2 会话中心经顶部 nav 互链；全部零构建、无外部 CDN（有测试守护：`test_pages_have_no_external_refs` / `test_pages_interlinked_nav`）。

## 质检工作台 `/app/workbench.html`（切片 4 落地 + 切片 6 交互重构，v3.0 §4.1 一期主干）

导航即工作流，workbench 排首位（默认页）。**向导式交互（说人话）**：

- **步骤条**：case 顶部四步指示（①复盘归因 ②修复 ③重放验证 ④完成），当前步高亮、已完成打勾、未到灰色；每步下方一句"这一步要做什么"的人话提示（①读会话判断错在哪；②选一种方式修掉它；③看重放答得好不好；④已归档进回归集）
- **会话回放气泡化**：customer 右蓝 / ai_agent 左白 / tool 折叠成 chip（"🔧 查了 \<工具名\>"点击展开入参出参）/ status 事件默认隐藏（开关"显示引擎过程"才出现）/ preamble 标注"AI 先说了一句占位话"
- **归因卡片化**："这条回答错在哪？"四张卡片各配人话+例子（知识库没这条规定 / 没教过 AI 怎么处理 / 工具没答上来 / 模型自己说错了），选中后才出现备注与提交
- **修复向导**：按归因类型给表单——knowledge 预填后直建知识补录草稿（`POST /api/knowledge/drafts`）并自动挂接；rule 预填 condition 提交 guideline 草稿自动挂接；tool 生成可复制工单文本；model 确认入对抗样本。提交成功提示"已挂接，去重放验证"并自动切步骤③
- **重放验证步**：一键 verify（loading 提示）→ 并排"当时的错答 vs 重放的新答"→ 两个大按钮"答得好，关闭"（close）/"还不行，重新修"（**新增 `POST /{id}/reopen`**：pending_verify→pending_fix，verify/fix_ref 保留供对比）
- **左栏**：四态队列（人话名称+计数徽章，空态文案"太棒了，没有待处理的 bad case"）+ "摄入转人工复盘"按钮 + 已读不回告警单列

**状态机**（BFF 强校验，违例 409）：

```
待复盘(pending_review) ──归因(四选一+prefill)──> 待修复(pending_fix)
待修复 ──挂接修复物(link-fix)──> 待验证(pending_verify)
待验证 ──沙盒重放(verify，不自动判过) + 人工确认(close)──> 已关闭(closed) + 入回归集
   ↑__________ 重放不满意打回重修(reopen) __________┘
```

## 知识库管理页 `/app/knowledge.html`（切片 6，v3.0 §5 知识编辑器）

顶部 nav 第三席（六页互链有测试守护）。三个标签页：

1. **文档浏览**：q 模糊搜索 + 类型过滤 + 状态过滤（生效/临期 7 天/已过期，对应 RAG `valid_state`）+ 分页表格（标题/类型/有效期/状态徽标），点击行右侧栏看全文（`GET /api/knowledge/docs/{id}`）
2. **缺口看板**：`gaps/stats` 四卡片（窗口期/总查询/未命中/占比）+ `gaps` 表格（问题/次数/最近出现），每行"补录"按钮带问题跳补录草稿表单（预填 question）
3. **补录草稿**：草稿按状态分组（待审核/已审待发布/已发布/已打回，含 bundle_asset_id 与发布后 docs 数展示）；reviewer 操作区"发布并重建索引（约 2 分钟）"（成功显示 docs 数，失败显示错误尾）与"打回"（带理由）；operator 新建草稿表单（question/suggested_answer 必填，wrong_answer 选填）

## 知识补录闭环（切片 5）

```
bad case 归因"知识缺失"（prefill: 问题/错误回答/建议答案位）
  → POST /api/knowledge/drafts（operator 补录草稿，pending_review）
  → POST /api/knowledge/drafts/{id}/approve（reviewer，双人复核）
      ① 生成 bundle 文件 BUNDLES_DIR/kb<时间戳hex>.md
         （asset_id=pvg.v2.kb<hex>，section=manual_supplement，source_uri=admin://badcase/<case_id>，
          knowledge_type=faq，valid 默认 3 个月，双 front-matter 与现有 890 篇同构）
      ② 调 RAG POST /reindex（超时 300s：子进程跑 build_knowledge_index.py 重建索引后自动热加载）
      ③ 成功 → status=published（记 bundle_asset_id 与 published_docs）
         失败 → 保持 approved + reindex_error 错误尾（bundle 文件不丢，运维可直接调 RAG /reindex 重试）
  → bad case link-fix ref_type=knowledge_draft 引用该 draft → verify → close
```

- bundle 生成器在 `admin/app/bundles.py`；生成产物已实测可被 `scripts/build_knowledge_index.py` 的 `parse_doc` 正确解析（id=文件 stem，knowledge_type=faq 进入索引）
- RAG 服务 `POST /reindex`：同步执行（约 1-3 分钟），锁防并发（重复调用 409），子进程 env 注入 `PVG_KNOWLEDGE_INDEX` 与 `PVG_BUNDLES_DIR`；`build_knowledge_index.py` 已支持这两个环境变量覆盖默认路径
- **compose 挂载（deploy/docker-compose.dev.yml）**：admin-bff `../knowledge/bundles:/data/knowledge/bundles:rw` + `BUNDLES_DIR=/data/knowledge/bundles/v2-miniprogram-20260719b`；pvg-rag 同路径 `:ro` + `PVG_BUNDLES_DIR` + `PVG_QUERY_LOG=/data/knowledge/index/query-log.jsonl`（查询日志落盘持久化）
- 宿主目录约定：`~/pvg-poc/knowledge/bundles/v2-miniprogram-20260719b/`（仓库 `knowledge/bundles/` 为其副本/链接，890 篇 .md）

## 与 v3.0 设计的对应关系及偏离登记

| 设计（v3.0） | 本实现 | 偏离/说明 |
|---|---|---|
| §4.1 质检闭环（一期主干） | 切片 4 全落地：intake（转人工复盘去重）、四类归因+prefill、link-fix 挂接、沙盒重放 verify、关闭硬条件+回归沉淀、no-reply 告警特例；切片 5 补齐知识补录闭环 | 人工抽检（manual_sample）与自动预警（红线/幻觉/负反馈）为二期；AI 辅助归因为后续切片 |
| §8 后端形态：FastAPI 当前实现，**生产置换 Java BFF**（接口契约不变） | **FastAPI 实现**（切片 1-5 验证 API 面、审批流、会话中心、沙盒、质检与知识闭环语义） | **已登记偏离**：Java 迁移时按本仓库 API 契约与测试用例对齐重写；权限/审批/审计语义不变 |
| §8 治理数据落 MongoDB（ADR-0004） | `store.py` 抽象 + Mongo 实现（惰性 pymongo），无 Mongo 时降级内存 | 内存降级仅供开发/测试，容器部署必须配 `MONGO_URL` |
| §3 RBAC（v3.0 §3） | dev 级：进程内令牌 + 环境变量用户表 | 生产需换 SSO/账号体系与签名令牌 |
| 审批流 + 双人复核（§5 编辑器群下游） | approvals 集合 + 状态机 + 起草人回避 | 版本快照/回滚、红线专区、冲突检测为后续切片 |
| §5 规则编辑器-在线试聊沙盒（生产快照+草稿叠加） | 切片 3 落地：test agent 复制 + 同 condition 覆盖 + ready/completed 判定 | **沙盒复制取 GET /guidelines 全量**（dev 环境单 agent 等价；多 agent 后应按 `agent:PROD_AGENT_ID` 标签过滤，登记遗留）；沙盒 session 复用为 BFF 进程内 map（多副本不共享） |
| §5 话术编辑器-渲染预览 | `/api/canned/preview` jinja2 StrictUndefined + 实时预览页 | 版本 diff/回滚、向量命中范围预览（候选阈值 0.4）为后续切片 |
| §5 知识编辑器（文档管理/召回测试台/缺口看板/热加载） | RAG `/docs` `/gaps` `/reload` 代理 + 三阶段召回页 + 补录草稿流（bundle+reindex） | 缺口看板前端页未做（API 已通）；文档生效/失效编辑（改 bundle 字段+reindex）为后续切片 |
| §4.3 会话中心（监控/接管/trace/SSE） | 切片 2 全部落地：工单视图列表、分页回放、takeover/代发+审计、SSE 直连 | **多副本 Redis pub/sub 转发未做（登记遗留）**；情绪倾向/转人工原因分类、已读不回突增告警为后续切片；preamble 判定为启发式 |
| §4.3 审计中心 append-only | 仅追加与查询，无更新/删除路由 | 导出文件、留存策略（≥3 年）为后续切片 |

## 遗留（切片 6+）

- **多副本 SSE 转发**：Redis pub/sub 把会话事件路由到持有对应前端连接的 BFF 副本（单 BFF 期直连即可）
- **知识库页面二期**：文档生效/失效编辑（改 bundle 字段+reindex）、草稿详情全文预览、缺口看板窗口切换 UI
- **approved（reindex 失败）草稿的重试路径**：当前只能运维直调 RAG `/reindex`；后续可加 `POST /drafts/{id}/retry-publish`
- **质检二期**：人工抽检按比例抽样（manual_sample）、自动预警检测器接入（红线/幻觉/负反馈）、AI 辅助归因建议
- **沙盒生产快照过滤**：多 agent 后 start 应按 `agent:PROD_AGENT_ID` 标签过滤再复制；沙盒 session 复用表进程内，多副本不共享
- **沙盒 stop 依赖上游删除接口**：无 `DELETE /agents/{id}` 时走"停用全部规则"降级（已登记），test agent 本体残留需上游支持后清理
- 会话中心增强：情绪倾向、转人工队列原因分类、已读不回突增告警、延迟分解视图对齐评测口径
- 变更发布（变更单、异步回归门禁、灰度/回滚，v3.0 §4.2 二期）
- 评测中心、数据看板（回归集已开始在 `regression_items` 沉淀，尚未接 evaluation 执行器）
- canned response 的 PATCH 审批流与版本 diff/回滚、槽位治理联动检查（工具侧 `canned_response_fields`）
- 审计导出文件与防篡改加固（如哈希链）

