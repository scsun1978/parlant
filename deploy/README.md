# PVG 智能客服 · dev03 单节点开发环境（容器部署）

部署目标：`ai-server (172.16.100.102 / netbird 100.80.76.96)`，面向管理后台开发，
按生产架构组件形态单点落地（对齐《生产网架构与部署方案》，差异见文末）。

## 组成（5 容器）

| 容器 | 端口 | 说明 |
|---|---|---|
| parlant-server | 8800 | 对话引擎（DeepSeek API；home=/data/parlant-data bind mount） |
| pvg-proxy | 8900 | MCP 工具网关：航班/POI/失物转发 + 知识检索转发（纯转发，无模型） |
| pvg-rag | 8901 | RAG 检索服务：`/search` `/stages`(FR-804 分阶段召回) `/reload`(热加载) `/health` |
| mongo | 27017 | 后台治理数据库（卷 mongo-data） |
| qdrant | 6333 | 向量库（卷 qdrant-data；供后台术语/知识管理，Parlant 侧切换为后续工作） |
| admin-bff | 9000 | 运营后台 BFF（FastAPI：RBAC/审批流/审计/沙盒/会话 SSE，挂载定制页 /app/） |
| appsmith | 8902 | 低代码 CRUD 页面平台（appsmith/appsmith-ce:release，页面搭建见 admin/appsmith-setup.md） |

## 操作

```bash
ssh scsun@172.16.100.102
cd ~/pvg-poc

# 启动/更新（代码改动先 rsync 到 ~/pvg-poc，再 build + up）
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml build
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml up -d

# 状态与日志
docker ps
docker logs -f parlant-server   # 或 pvg-proxy / pvg-rag

# 从本机健康检查
curl http://172.16.100.102:8901/health
curl http://172.16.100.102:8800/agents
```

## 数据与代码同步（本机 → dev03）

```bash
rsync -az src pyproject.toml README.md deploy scripts <host>:~/pvg-poc/
rsync -az parlant-data <host>:~/pvg-poc/                 # 引擎数据（可选）
rsync -az --exclude bundles --exclude releases --exclude reviews knowledge <host>:~/pvg-poc/
rsync -az ~/.cache/huggingface/hub/models--jinaai--* <host>:~/pvg-poc/hf-cache/hub/   # HF 被墙，模型随包走
```

注意：
- **server 重启后 glossary 丢失**（瞬态向量库）：在本机执行
  `PARLANT_BASE_URL=http://172.16.100.102:8800 .venv/bin/python scripts/seed_pvg_poc_data.py`
- parlant-data/services.json 的 pvg 工具地址在 dev03 必须是 `http://pvg-proxy:8900`（compose DNS）
- 知识索引更新：重建后 rsync `knowledge/index`，再 `curl -X POST http://172.16.100.102:8901/reload`（无需重启）
- 容器内存：parlant-server 6g 限额不可再降——启动期 torch+Jina 峰值超 4g 会被 cgroup OOM（实测）

## 与生产架构的差异（登记）

1. LLM 用 DeepSeek 线上 API（生产为自托管 GPU；dev03 无 GPU 且 15G 内存）——勿向该环境导入真实旅客数据
2. Parlant 持久化仍为 JSON 文档库 + 瞬态向量库（生产为 MongoDB + Qdrant；mongo/qdrant 容器已就位，引擎侧切换为后续工作）
3. 无 API 网关/RBAC（管理后台开发对象本身）
4. 无 ES（RAG 服务暂用 in-process BM25，/stages 接口形态与生产一致，后端可平滑换 ES）
5. 无监控栈/MinIO（后台开发需要时再加）
