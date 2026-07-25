# Appsmith 配置指南（低代码 CRUD 页面）

> 对应《管理后台设计方案 v2.1》§6 页面划分：以下页面由 Appsmith 拼装；
> 会话中心（`http://dev03:9000/app/`）、召回测试台（`/app/rag.html`）、
> 试聊沙盒（`/app/sandbox.html`）、话术预览（`/app/canned-preview.html`）为定制页，不在此列。
>
> Appsmith 地址：`http://172.16.100.102:8902`（容器 pvg-appsmith，社区版自托管）

## 1. 数据源

| 数据源 | 类型 | 配置 |
|---|---|---|
| `admin-bff` | REST API | URL `http://admin-bff:9000`（Appsmith 服务端调用，走 pvg 内网）；认证：无（token 由各查询页面注入 header，见下）；CORS 已放通（BFF 侧 `ADMIN_CORS_ORIGINS`） |
| `admin-mongo`（只读看板可选） | MongoDB | `mongodb://mongo:27017`，库 `pvg_admin`，集合 `approvals` / `audit_logs` |

**登录与 token**：BFF 的 API 需要 `Authorization: Bearer <token>`。做法：在 Appsmith 建一个登录页（POST `admin-bff` 的 `/api/auth/login`，body `{"username","password"}`），把返回 token 存入 `appsmith.store.token`，各查询的 header 引用 `{{appsmith.store.token}}`。

开发期五角色账号（compose 注入，生产须换）：`admin/admin123`(admin)、`yunying/pvg2026`(operator)、`shenhe/pvg2026`(reviewer)、`shenji/pvg2026`(auditor)、`banzhang/pvg2026`(supervisor)。

## 2. 起始页面清单（按优先级）

### P1 审批队列（reviewer 主战场）

- 表格：GET `/api/approvals?status=pending_review`（列：id/type/作者/时间/摘要）
- 详情侧栏：GET `/api/approvals/{id}`（展示 before/after 与 payload 原文，JSON viewer）
- 按钮：`POST /api/approvals/{id}/approve`、`POST /api/approvals/{id}/reject`（reject 弹窗填理由）
- 权限提示：approve/reject 需 reviewer/admin token；operator 只可查看

### P2 规则列表与起草（operator）

- 表格：GET `/api/guidelines`（列：condition/action/criticality/enabled/tags）
- 起草表单：双栏（当/则）+ 关键级 + 组合模式（下拉：继承/fluid/canned_fluid/composited_canned）+ 工具绑定多选 → POST `/api/guidelines/draft`
- 提示文案：「提交后进入审核队列，审核员复核后才写入引擎」

### P3 话术列表与起草（operator/reviewer）

- 表格：GET `/api/canned-responses`（列：value/fields/signals/tags）
- 起草表单：value(jinja2) + fields JSON → POST `/api/canned-responses/draft`
- 预览入口：iframe 嵌入定制组件 `http://172.16.100.102:9000/app/canned-preview.html`

### P4 审计查询（auditor）

- 筛选（actor/action/时间范围）+ 分页表格：GET `/api/audit-logs?actor=&action=&limit=&offset=`
- 导出按钮：拉全量下载 CSV（Appsmith `download()`）

### P5 知识缺口看板（运营）

- RAG 健康卡：GET `/api/rag/health`（docs 数）
- 召回测试入口：iframe 嵌入 `http://172.16.100.102:9000/app/rag.html`
- （后续接 bad case 工作台数据）检索为空问题 Top N 表

### P6 术语表（operator）

- 表格：GET `/api/terms`；新建表单：POST `/api/terms`（name/description/synonyms）
- 提醒文案：「术语存引擎瞬态库，server 重启后需重跑种子脚本补种（迭代 2 切 Qdrant 后解除）」

## 3. 验收清单（页面搭建后）

- [ ] 五角色分别能登录，权限正确（operator 不能 approve）
- [ ] 起草一条 guideline → 出现在审核队列 → reviewer approve 后出现在规则列表（引擎生效）
- [ ] reject 后引擎无该规则
- [ ] 审计页能查到上述全部操作记录（actor/action/before/after）
- [ ] P3 iframe 预览组件可渲染 `{{knowledge_answer}}` 样例
