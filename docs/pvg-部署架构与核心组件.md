# PVG 智能客服 PoC · 部署架构与核心功能组件

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-07-20 |
| 状态 | 反映 PoC 当前**已建成**的部署形态（非 PRD 目标生产架构，差异见 §6） |
| 关联文档 | 《机场智能客服PRD》第 3 章、《使用与测试手册》、《PoC 验证报告》 |

---

## 1. 部署拓扑（当前：macOS 单机全本地）

```
┌────────────────────────────────────────────────────────────────┐
│ 本机（macOS，128GB）                                            │
│                                                                │
│  测试客户端（curl / run_eval.py / 手册 §2.1 脚本）              │
│        │ REST（development auth，无鉴权）                       │
│        ▼                                                       │
│  ┌─────────────────────┐   MCP over HTTP    ┌────────────────┐ │
│  │ Parlant server      │  127.0.0.1:8900/mcp│ PVG MCP 代理    │ │
│  │ :8800 (0.0.0.0)     │◄──────────────────►│ :8900 (127.0.0.1)│ │
│  │ parlant.bin.server  │   （server 缓存     │ FastMCP 3.2.4   │ │
│  │ run --deepseek      │    session，代理    │                 │ │
│  │                     │    重启后须重启     │                 │ │
│  │ home=parlant-data/  │    server）        │                 │ │
│  └──────┬──────────────┘                    └───┬────┬────┬───┘ │
│         │                                       │    │    │     │
│   DeepSeek 线上 API                    HMAC 签名 │    │    │ OAuth
│   （对话生成，按量付费）                        │    │    │ client-credentials
│         ▼                                       ▼    ▼    ▼     │
│  ┌──────────────┐   ┌────────────────────────────────────────┐ │
│  │ api.deepseek │   │ PVG 真实后端（腾讯云测试环境）            │ │
│  │ .com         │   │ 航班 Streamable MCP / POI / 失物 REST    │ │
│  └──────────────┘   └────────────────────────────────────────┘ │
│                                                                │
│  本地模型（HuggingFace 缓存，CPU 推理，无外呼）：                │
│  · jina-embeddings-v3（server 侧：术语/话术向量）               │
│  · jina-embeddings-v2-base-zh（代理侧：知识检索向量 + jieba）   │
└────────────────────────────────────────────────────────────────┘
```

**端口与进程**

| 进程 | 端口 | 启动方式 | 日志 |
|---|---|---|---|
| Parlant server 3.3.1 | 8800 | `scripts/run_parlant_server.sh [--with-seed]` | `logs/parlant-server.log` |
| PVG MCP 代理 | 8900 | `scripts/run_pvg_proxy.sh` | `logs/pvg-proxy.log` |

启动顺序约束：先代理后 server；**代理重启必须连带重启 server**（MCP session 不自动重连）。

## 2. 核心功能组件

### 2.1 Parlant server（对话引擎，`xyVHBNLLPg` 浦东机场智能客服）

| 子组件 | 当前配置 | 说明 |
|---|---|---|
| Agent | max_engine_iterations=3，composition=`canned_fluid` | fluid 生成 + 命中模板时用模板 |
| Guideline 规则 | 29 条（含红线 1 条、Journey 触发 9 条、Journey 步骤 6 条） | 覆盖航班/值机/行李/政策/交通/停车/设施/投诉/失物/拒答/机场字典/延误安抚 |
| 高合规组合模式 | 3 条 guideline 为 `composited_canned`（FR-203） | 航班/行李/政策：模板骨架 + 工具/RAG 动态槽位 |
| Canned 话术模板 | 10 条（7 固定 + 3 动态槽位） | 模板与槽位字段经向量检索候选（阈值 0.4） |
| Journey 流程 | 3 个：失物招领/投诉与转人工/退改签引导 | trigger guideline 自动建，步骤为 journey tag 规则 |
| Glossary 术语 | 16 条民航/机场术语 | **内存瞬态向量库，重启丢失，须补种** |
| 条件式工具供给 | 工具与 guideline 关联（FR-404） | 未关联的规则命中时模型看不到工具 |
| 模型层 | DeepSeek 线上 API（NLP）+ 本地 Jina v3（嵌入） | 嵌入缓存 `cache_embeddings.json` |

### 2.2 PVG MCP 代理（`scripts/pvg_mcp_proxy.py`，306 行）

封装真实后端 + 内嵌混合检索，暴露 7 个工具：

| 工具 | 上游 | 认证 |
|---|---|---|
| pvg_flight_search / pvg_flight_detail / pvg_airport_search | 航班 Streamable MCP（腾讯云） | HMAC-SHA256 每请求签名 |
| pvg_poi_search / pvg_lost_and_found_search | POI / 失物招领 REST | OAuth client-credentials（token 缓存 10 分钟） |
| pvg_current_datetime | 本地 | — |
| pvg_knowledge_search | **本地知识索引**（BM25+向量+RRF k=60，top5） | — |

- 航班/知识工具返回带 `canned_response_fields`（FR-203 动态槽位声明）
- 已知容错：`page/pageSize` 容忍显式 null；`pvg_flight_detail` 因上游故障走 search 兜底并标注

### 2.3 知识管线（离线）

```
OpenViking（腾讯云，源系统）
  → knowledge/bundles/（888 篇双 front-matter md，含有效期/scope）
  → knowledge/reviews/（迁移评审 + 适用性评审快照）
  → knowledge/releases/（不可变发布快照 + tarball，sha256）
  → scripts/build_knowledge_index.py（jieba 分词 + Jina v2-zh 本地嵌入）
  → knowledge/index/（docs.jsonl + embeddings.npy）
  → 代理启动时加载（改索引须重启代理 → 连带重启 server）
```

### 2.4 数据种子与评测

| 组件 | 文件 | 说明 |
|---|---|---|
| 数据种子 | `scripts/seed_pvg_poc_data.py` | 术语/话术/Journey/红线/工具关联/组合模式，幂等可重跑 |
| 评测套件 | `evaluation/`（corpus_v1 66 条 → 扩增 520 条、run_eval.py、generate_corpus.py、merge_reports.py、corpus_strict 27 条） | must_include/must_not_include/expected_tools 三维 + 延迟 |
| 分块压测 | `scripts/run_eval_chunked.sh` + `evaluation/chunks/` | 健康检查 + 失败自动拉起 |
| 运维脚本 | `scripts/run_pvg_proxy.sh`、`run_parlant_server.sh` | 凭据统一 `scripts/pvg_proxy.env`（0600，gitignored） |

## 3. 数据持久化（`parlant-data/`）

| 数据 | 载体 | 重启后 |
|---|---|---|
| agent / guideline / Journey / 工具服务 / 会话 | `*.json` 文档库 | ✅ 保留 |
| 话术模板 | `canned_responses.json` | ✅ 保留 |
| 术语（glossary） | 内存瞬态向量库 | ❌ 丢失（`--with-seed` 补回） |
| 嵌入缓存 | `cache_embeddings.json` | ✅ 保留（补种秒级的原因） |
| `chroma.sqlite3` | 仅迁移工具使用 | 当前为空，运行期不使用 |

## 4. 外部依赖与凭据

| 依赖 | 用途 | 凭据位置 |
|---|---|---|
| DeepSeek API | 全部 LLM 生成（guideline 匹配/工具推断/答复/revision） | `scripts/pvg_proxy.env` |
| 航班 MCP（`pvgminiprogram.pvgaerodrome.com/mcp/flight`） | 航班实时数据 | 同上（App ID/Key） |
| POI / 失物 REST（同域名） | 设施位置 / 拾获记录 | 同上（Client ID/Secret） |
| OpenViking 知识库（腾讯云内网） | 源知识系统（PoC 已导出，运行期不依赖） | 指南文档（0600） |
| HuggingFace（jina 模型） | 首次下载后走本地缓存 | 无需凭据 |

## 5. 已知运维约束（实测登记）

1. **MCP session 不自动重连**：代理重启 → server 必须重启。
2. **glossary 瞬态**：server 重启 → 术语丢失 → `--with-seed` 补种。
3. **长跑稳定性**：两次 ~40 分钟高并发压测中 server/proxy 静默死亡（无崩溃报告，根因未明）；分块执行可控制损失。生产前须定位并加进程守护。
4. **bash 解析坑**：本机 bash 5.3 对 `$变量` 后紧跟全角字符解析异常，脚本一律用 `${变量}`。
5. **OneDrive 路径**：项目目录在云同步盘内，长压测/大量写文件时注意同步干扰（未证实与崩溃相关，仅登记）。

## 6. 与 PRD 目标架构的差异（本期未建）

| PRD 目标 | 当前状态 |
|---|---|
| Java 业务后端（会话映射/鉴权/渠道适配/工单） | 未建，测试直连 Parlant REST |
| MongoDB 持久化 | 未启用，使用默认 JSON 文档库 + 瞬态向量库 |
| RAG 独立微服务 | 检索内嵌在代理进程（单体内实现） |
| 管理后台（RBAC/审批/版本/看板四件套） | 未建，运营靠种子脚本 + REST |
| 自托管模型（线 0） | 未建，用 DeepSeek 线上 API（数据出域，仅 PoC 阶段可接受） |
| 转人工工单 / 小程序渠道接入 | 仅 API 级验证（session manual 模式），未接真实渠道 |
