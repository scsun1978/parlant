# Appsmith 应用构建说明（内部 API 实测配方）

> 应用：**PVG 客服运营后台**（已 publish）
> View：`http://172.16.100.102:8902/app/6a6340d71e2ed4ecb09747ed`
> Edit：`http://172.16.100.102:8902/applications/6a6340d71e2ed4ecb09747ed`
> 管理员：`admin@pvg.com.cn / Admin@2026`（Appsmith 自有账号体系，与 BFF 五角色无关）

## 页面与内容

| 页面 | 内容 | 对应 action |
|---|---|---|
| 使用说明 | 使用流程与五角色账号说明（默认首页为"登录"） | — |
| 登录 | 用户名/密码输入 + 登录按钮（token 存入 appsmith.store.token）+ 登录状态 | loginApi |
| P1-审批队列 | 待审表格 + 详情 JSON + 通过/打回（含理由输入） | qApprovals / qApprove / qReject |
| P2-规则中心 | guideline 表格 + 起草表单（condition/action/关键级/组合模式） | qGuidelines / qDraftGuideline |
| P3-话术中心 | canned 表格 + jinja2 起草 + iframe 话术预览组件 | qCanned / qDraftCanned |
| P4-审计查询 | 操作人筛选 + 查询 + 导出 CSV + 表格 | qAudit |
| P5-知识看板 | RAG 健康卡片 + iframe 召回测试台 | qRagHealth |
| P6-术语表 | terms 表格 + 新建表单 + 瞬态提醒 | qTerms / qCreateTerm |

数据源：REST API `admin-bff`（`http://admin-bff:9000`，Appsmith 容器内网可达，已实测 healthy）。所有查询 header：`Authorization: Bearer {{appsmith.store.token}}`。

## 内部 API 实测配方（重建/扩展时用）

1. 登录：`POST /api/v1/login`（form 编码 username/password，header 带 X-XSRF-TOKEN（从 cookie）与 X-Requested-By: Appsmith）→ SESSION cookie
2. 对象端点（均需 SESSION + 两 header）：
   - `POST /api/v1/applications` {name, workspaceId}
   - `POST /api/v1/pages` {name, applicationId, **layouts:[{dsl}] 必填**}
   - `POST /api/v1/datasources` {name, pluginId, workspaceId, datasourceConfiguration:{url}}（409 时先查重：GET /api/v1/datasources?workspaceId=）
   - `POST /api/v1/actions` {name, pageId, pluginId, pluginType:"API", datasource:{id}, actionConfiguration:{httpMethod,path,headers,queryParameters,body,timeoutInMillisecond,encodeParamsToggle}, executeOnLoad}
   - `PUT /api/v1/layouts/{layoutId}/pages/{pageId}?applicationId={appId}` {dsl}（**applicationId 必须作 query 参数**）
   - `POST /api/v1/applications/publish/{appId}`（发布）
   - `PUT /api/v1/applications/{appId}/page/{pageId}/makeDefault`（设默认页）
   - `PUT /api/v1/pages/{pageId}`（改名）
3. 插件 id：REST API = `6a6102271e2ed4ecb09747a3`；MongoDB = `6a6102271e2ed4ecb09747a4`（GET /api/v1/plugins?workspaceId= 可查）
4. queryParameters 元素必须是 `{"key","value"}` 对象（数组会 400 Malformed parameter）

## 已验证

- 7+1 页面、12 个 action、布局 widget 树均经 consolidated-api 核实存在；应用已 publish（data:true 二次确认）
- Appsmith→BFF 网络：docker exec pvg-appsmith curl http://admin-bff:9000/health 返回 ok
- 登录→审批→查询链路：绑定（`{{appsmith.store.token}}`）在浏览器端求值，需用户先在"登录"页登录（token 存浏览器后全部查询带 Authorization）

## 遗留

- token 存浏览器 store，多用户各自登录互不影响；BFF token 进程内有效（BFF 重启后需重新登录）
- 登录页账号为开发期演示做法（生产应改表单输入、不落明文）
- Appsmith CE 的 SSO/RBAC 为企业版功能，页面级权限依赖 BFF 侧 RBAC（登录角色决定可调用的 API）
- P2 起草表单暂未含工具绑定多选（BFF 已支持 tool_associations，页面后续补）
