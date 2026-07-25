# MCP 服务改进与设计方案

> 版本：v1.0 草案 | 日期：2026-07-21
> 依据：代码调研（`src/parlant/core/services/tools/`）+ PRD/部署/PoC 文档需求提取
> 关联文档：机场智能客服PRD.md §4.4/§6.3、生产网架构与部署方案.md、PoC验证报告-20260720.md、real-mcp-call-guide-20260719.md

---

## 1. 项目对 MCP 的需求（Requirement Analysis）

从 PRD 与部署文档中提取的 MCP 相关需求，按来源归类：

### 1.1 功能性需求

| 编号 | 需求 | 来源 |
|---|---|---|
| R1 | 业务工具经 MCP 暴露：航班查询、订单、退改签、失物招领等既有 Java 接口包装为 MCP server，Parlant 内置客户端接入 | 机场智能客服PRD.md 架构决策3、FR-401~403 |
| R2 | 条件式工具供给：工具经 guideline 的 tool_associations 绑定，仅在 guideline 命中时可见 | FR-404（已实现于 service_registry + guidelines） |
| R3 | 缺参反问：工具参数缺失时引擎能反问旅客补齐 | FR-405（依赖 ToolParameter 元数据完整性） |
| R4 | 复杂 schema 类型映射正确性（POC 验证项）：上游 Java 封装的 MCP 工具 schema 必须被 parlant 正确映射 | PRD §6.3 |
| R5 | 执行类工具（退改签等）需旅客显式确认后才调用 | FR-402（依赖工具元数据标记"执行类"） |

### 1.2 非功能性需求

| 编号 | 需求 | 来源 |
|---|---|---|
| R6 | **工具调用成功率 ≥98%**，跌破告警 | PRD KPI、FR-406 |
| R7 | **MCP 工具网关化 ×2 + 熔断降级**：PoC 出现上游超时与 "Session terminated" 连锁故障；网关注入健康检查/重试/熔断，工具失败时给友好降级口径 + 转人工 | 生产网架构与部署方案.md :43-44, :80, :150 |
| R8 | 生产网零出域；凭据收敛管理、泄露可轮换 | 生产网架构 :157；测试手册 :199 |
| R9 | 管理后台可观测：工具清单（名称/来源 server/绑定 guideline/7 日调用量与成功率）、健康监控（<98% 标红）、沙盒连通性测试、版本锁定提示 | 管理后台设计方案 §4.6 |
| R10 | 上游异构统一：航班（Streamable MCP + 每请求 HMAC 签名）、POI/失物（REST + OAuth CC）、知识库（非 MCP）统一为单一 MCP server 供 parlant 消费 | real-mcp-call-guide；pvg-部署架构 :68-80 |
| R11 | fastmcp/MCP 版本锁定，薄封装可整体替换 | PRD 风险表 :370 |

### 1.3 已确认的运行事实

- 上游航班服务是 **stateless Streamable HTTP MCP**（协议 2025-06-18），每请求重新生成毫秒时间戳 + HMAC-SHA256 签名，不需要 initialize / Mcp-Session-Id（real-mcp-call-guide :7-17）。
- PoC 曾出现"MCP session 无自动重连，代理重启后 server 报 Session terminated"（PoC报告 :60）。**当前代码已含重连/重试逻辑（mcp_service.py:183-290），该文档记载为旧行为，文档需更新**。
- PoC 正常运行期工具链调用全部成功（PoC报告 :22）。

---

## 2. 现状与差距分析（Gap Analysis）

### 2.1 当前架构

```
Parlant server
  └─ MCPToolClient（仅 Streamable HTTP，无鉴权，url 唯一配置）
       └─ pvg_mcp_proxy.py（FastMCP server，127.0.0.1:8900/mcp，7 工具）
            ├─ 腾讯云航班 MCP（HMAC 签名，代理代签）
            ├─ POI/失物 REST（OAuth CC，代理代取 token）
            └─ RAG 检索服务（HTTP 转发）
```

注册路径：REST `PUT /services/{name}`（kind=mcp，仅 url）→ `ServiceDocumentRegistry.update_tool_service` → 持久化文档库 → 启动时全量恢复重连。工具可见性经 guideline tool_associations（seed 脚本统一挂 service_name "pvg"）。

### 2.2 差距清单（按需求倒排）

| 需求 | 现状 | 差距 | 证据 |
|---|---|---|---|
| R6/R7 成功率≥98% + 熔断 | proxy 有 3 次重试/45s 超时；parlant 侧 `call_tool` **不走** `_with_reconnect`，调用中途掉线直接抛 ToolError | **无熔断器、无健康检查、无成功率指标；call_tool 不重试** | mcp_service.py:324-331 vs :272-290 |
| R7 网关化×2 | proxy 是单进程脚本，无健康端点、无指标、无优雅停机 | proxy 需升级为正式网关服务（HA、/health、/metrics、熔断、降级口径） | scripts/pvg_mcp_proxy.py；部署方案 :94 |
| R1 上游签名/OAuth | parlant 客户端**无鉴权/header/TLS/超时配置入口**，API DTO 只有 url——这是 proxy 存在的根本原因 | MCPToolClient 需支持 headers/auth/timeout，使简单场景可不经 proxy 直连 | mcp_service.py:178；api/services.py:116-127 |
| R4 复杂 schema | object 降级为 string、union 仅 Optional、enum 仅 string、`$ref` 仅本地 | Java 封装工具若用 object 参数/非 string enum 将映射错误或报错 | mcp_service.py:372-414, :485-516 |
| R3/R5 参数元数据 | MCP 工具固定 `ToolParameterOptions()` 空默认、`consequential=True`、`overlap=ALWAYS` | 无法用 hidden/source/precedence/adapter 做缺参反问优化；无法标记执行类工具供确认门控 | mcp_service.py:353, :362-363 |
| R2 | 已实现（guideline 关联） | — | — |
| R8 | proxy 收敛凭据（env 0600）符合；parlant 侧无凭据 | 符合，但 parlant 直连模式引入凭据后需 Secret 管理设计 | 测试手册 :199 |
| R9 | 无任何调用埋点 | 需新增工具调用指标（按 service/tool 的成功率、延迟）供后台消费 | 管理后台 §4.6 |
| R10 | proxy 已实现统一包装 | 符合；需随网关化重构保留 | pvg_mcp_proxy.py |
| R11 | fastmcp >=3.2.0 已锁定 | 符合（CHANGELOG :38, :92） | — |
| — 其他缺陷 | stdio/SSE 客户端不支持；endpoint 路径写死 `/mcp`；URL 端口解析脆弱（`url[-6:]`）；`read_tool` 无缓存每次全量 list_tools；`FormatError` 误从 `mailbox` 导入；SDK 无 MCP 入口；文档三处"不自动重连"记载已过时 | 见 §4 改进项 | mcp_service.py:155-178, :20, :301-315 |

---

## 3. 目标架构（Design）

分层改进：**L1 Parlant 核心客户端增强**、**L2 PVG MCP 工具网关**（proxy 升级）、**L3 可观测与管理**。原则（PRD 风险表）：MCP 工具层薄封装，任何一层可整体替换。

```
┌────────────────────────────── Parlant server ──────────────────────────────┐
│  L1 MCPToolClient v2                                                        │
│   · transport: streamable-http（默认）/ stdio / sse                          │
│   · 连接配置: url+path / headers(含 Secret 引用) / connect_timeout /         │
│     call_timeout / retry 策略                                               │
│   · call_tool 统一走 _with_resilience（重连+幂等重试+超时）                   │
│   · schema 缓存 + 失效刷新；ToolParameterOptions/consequential 可由 server   │
│     侧 tool annotations / 注册参数覆盖                                      │
└──────────────────────────────┬─────────────────────────────────────────────┘
                               │ MCP (streamable-http, loopback/内网)
┌──────────────────────────────▼─────────────────────────────────────────────┐
│  L2 PVG MCP Gateway（pvg-airport-services ×2，8C16G→纯转发后可降配）          │
│   · FastMCP server + /health（上游连通性聚合）+ /metrics（Prometheus）        │
│   · 每上游: 熔断器（30s 半开）/ 超时 / 有限重试 / 降级响应（含 canned 口径    │
│     与 canned_response_fields，供 FR-406 与转人工）                         │
│   · 凭据统一管理: HMAC 签名器 / OAuth CC token 缓存 / env 注入              │
│   · 优雅停机: drain 进行中调用 → 关闭上游连接                                │
└───────┬──────────────────┬──────────────────────┬──────────────────────────┘
        │ MCP+HMAC         │ REST+OAuth           │ HTTP
   航班上游            POI/失物上游            RAG 检索服务
┌──────────────────────────────┴─────────────────────────────────────────────┐
│  L3 可观测: 工具调用埋点（service/tool/成功/延迟/错误码）→ 指标导出 →         │
│   管理后台工具清单与健康监控（<98% 标红）、沙盒连通性测试                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. L1：MCPToolClient 增强设计

### 4.1 连接配置模型（替代裸 url）

```python
@dataclass
class MCPConnectionConfig:
    transport: Literal["streamable-http", "stdio", "sse"] = "streamable-http"
    url: str | None = None            # http 系：完整 URL，含 path（不再写死 /mcp）
    command: list[str] | None = None  # stdio
    headers: Mapping[str, str] | None = None   # 支持 "${ENV_VAR}" 占位符引用，凭据不落库明文
    connect_timeout: float = 10.0
    call_timeout: float = 60.0        # 对齐插件侧 PARLANT_TOOL_TIMEOUT 语义
    max_connect_attempts: int = 3
```

- **URL 解析改用 `urllib.parse`**，废弃 `url[-6:]` 手工拆分（修 mcp_service.py:155-161）；endpoint path 可配置，兼容非 `/mcp` 的 server。
- **headers 占位符**：DTO 与文档库存 `${VAR}`，运行时从环境解析——满足 R8 凭据不落库、可轮换。
- **API/SDK/CLI 同步扩展**：`MCPServiceParamsDTO` 增加 `headers/connect_timeout/call_timeout/transport`（api/services.py:116-127）；CLI `service create --kind mcp` 增加对应选项；SDK 增加 `server.add_mcp_service(...)` 入口（当前 sdk.py 无任何 MCP 引用）。
- **stdio/sse**：复用 fastmcp 的 `StdioTransport`/`SSETransport`；stdio 是 MCP 生态主流形态（本地 Java 团队调试、第三方 server），成本低收益高。

### 4.2 调用韧性（R6/R7 的 parlant 侧一半）

- `call_tool` 与 `list_tools`/`read_tool` 统一走 `_with_resilience`：会话失效类异常（现 `_is_reconnectable` 集合）→ 重建会话重试一次；超时类按 `call_timeout` 截断。
- **幂等约束**：仅对标记非执行类（non-consequential）的工具自动重试；执行类工具（退改签）失败即抛，重试决策交给引擎的确认门控——与 R5 呼应。
- `read_tool`/`resolve_tool` 增加 schema 缓存（启动 list_tools 一次 + TTL/手动刷新），消除每次全量 list（mcp_service.py:301-307）。

### 4.3 Schema 映射补强（R4/R3/R5）

1. **object 参数不再降级为 string**：映射为 parlant 的嵌套参数结构（parlant ToolParameter 支持 object 描述符则递归映射；若不支持，最低限度保留 properties 到 description，避免信息丢失）。
2. **union 支持多类型取公共超集或按 JSON Schema `anyOf` 逐分支映射**；enum 支持 number/integer。
3. **`$ref` 支持带路径解析与循环检测**。
4. **工具元数据透传**：MCP tool 的 `annotations`（如 `destructiveHint`、`readOnlyHint`）映射到 parlant `consequential`；新增注册级覆盖参数（service 创建时按工具名指定 consequential/ToolParameterOptions），让执行类确认门控（R5）与参数洞察（R3）在 MCP 工具上可用。
5. 修正 `FormatError` 导入来源（mcp_service.py:20，误从 mailbox 导入）。

### 4.4 L1 验收标准

- 新增测试：stdio 传输注册-调用；headers 注入与 `${ENV}` 解析；call_tool 中途断连重试；call_timeout 生效；object/union/非 string enum/嵌套 $ref 映射；annotations→consequential 映射。
- 更新三份过时文档（PoC报告 :60、部署架构 :51/:125、手册 :190）中"session 不自动重连"的表述；tools.md 补 MCP 使用文档。

---

## 5. L2：PVG MCP Gateway 设计（pvg_mcp_proxy.py 升级）

定位：从"PoC 脚本"升级为生产网 PRD 要求的 **MCP 工具网关 ×2 + 熔断降级**（部署方案 :43-44, :94, :150）。保留现有 7 工具与凭据收敛职责不变，新增：

### 5.1 韧性组件（每上游独立配置）

```yaml
upstreams:
  flight:
    kind: mcp
    url: https://pvgminiprogram.pvgaerodrome.com/mcp/flight
    auth: {type: hmac-sha256, key_env: PVG_FLIGHT_KEY}
    timeout_s: 45
    retries: 3
    circuit_breaker: {failure_threshold: 5, window_s: 60, half_open_after_s: 30}
    fallback: {mode: friendly_message, tool_specific: {pvg_flight_detail: via_search}}
  poi_lostfound:
    kind: rest
    auth: {type: oauth2-client-credentials, token_cache_s: 600}
    circuit_breaker: {failure_threshold: 5, window_s: 60, half_open_after_s: 30}
```

- **熔断器**：失败率超阈值 → open（30 s）→ 半开单探针 → 恢复/再开。open 期间工具返回结构化降级响应：`{status: "degraded", reason, canned_response_fields: {...}}`，引擎按 FR-406 给友好口径并可转人工。
- **降级响应必须仍是合法 MCP result**（IsError=false + 结构化内容），避免 parlant 侧抛 ToolError 打断对话——这是"降级口径"能落地的关键设计点。
- **健康检查**：`GET /health` 聚合各上游状态（ok/degraded/down + 熔断状态 + 最近一次探测延迟），供 LB/运维与双实例互备。
- **优雅停机**：SIGTERM → 停止接新调用 → drain 进行中调用（上限 15 s）→ 关上游连接。

### 5.2 指标（R9 数据源）

Prometheus `/metrics`：
- `pvg_tool_calls_total{service,tool,result}`（result: ok/degraded/error）
- `pvg_tool_call_duration_seconds{tool}` 直方图
- `pvg_upstream_circuit_state{upstream}` gauge
- `pvg_upstream_calls_total{upstream,result}`

7 日调用量与成功率由管理后台从指标系统聚合（R9）。

### 5.3 双实例与部署

- 两实例无状态（token 缓存可进程内），LB 或 parlant 侧主备 URL。
- 部署顺序文档更新：代码已支持断线重连，"代理重启必须连带重启 server"的约束解除，改为"网关重启后 parlant 自动重连，期间调用短暂失败按降级处理"。
- 凭据沿用 env 注入 + 文件 0600；新增凭据轮换 runbook 引用（手册 :199）。

---

## 6. L3：可观测与管理后台对接

| 能力（R9） | 数据来源 | 实现要点 |
|---|---|---|
| 工具清单（名称/来源 server/绑定 guideline） | parlant REST `/services` + `/guidelines` | 后台只读聚合，无 parlant 改动 |
| 7 日调用量与成功率、<98% 标红 | 网关 /metrics（§5.2） | 后台接指标系统；阈值 98% 可配置 |
| 沙盒连通性测试 | 网关 /health + 管理后台发起单工具试调 | 试调走独立"沙盒"工具调用路径，不计入成功率 |
| 版本锁定提示 | 网关启动报告 fastmcp/上游协议版本 | /health 返回版本字段 |

---

## 7. 实施计划（对齐 PRD 里程碑）

| 阶段 | 内容 | 对应需求 | 预估 |
|---|---|---|---|
| P0（本周） | L1 快速修复：call_tool 重连重试、URL 解析、FormatError 导入、schema 缓存；文档过时表述更新 | R6 直接止损 | 1-2 天 |
| P1（W3-4，对齐"打通 MCP 航班查询工具"） | L1：headers/超时配置 + API/CLI 扩展；L2：网关熔断器 + /health + /metrics + 降级响应 | R1/R6/R7/R9 | 1 周 |
| P2 | L1：stdio/sse、schema 映射补强（object/union/enum/$ref）、annotations→consequential、SDK 入口 | R3/R4/R5 | 1 周 |
| P3 | L2 双实例部署 + 管理后台对接 + 演练（熔断 30s 半开、转人工口径） | R7/R9 | 与生产网上线同步 |

**风险**：fastmcp 生态变动（PRD 风险表）——L1/L2 均保持薄封装；schema 映射补强需以 Java 团队实际工具 schema 为输入做兼容验证（R4 POC 项）；headers 凭据引入后需补 Secret 管理评审（R8）。

---

## 8. 附：关键代码位置索引

| 主题 | 位置 |
|---|---|
| MCP 客户端 | src/parlant/core/services/tools/mcp_service.py:141-553 |
| 重连/重试 | mcp_service.py:183-290（call_tool 未覆盖：:324-331） |
| Schema 映射 | mcp_service.py:335-530 |
| 服务注册/持久化 | src/parlant/core/services/tools/service_registry.py:50, :260-351 |
| REST API | src/parlant/api/services.py:55, :116-127, :376-410 |
| CLI | src/parlant/bin/client.py:963-968, :4227, :4257 |
| 现有网关脚本 | scripts/pvg_mcp_proxy.py（HMAC :52-63；OAuth :105-116；重试 :75-90；兜底 :125-155） |
| 现有测试 | tests/core/stable/engines/alpha/test_mcp.py；tests/core/stable/services/tools/test_mcp_client.py |
