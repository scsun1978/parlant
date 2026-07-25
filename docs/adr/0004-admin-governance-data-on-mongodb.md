# 后台治理数据存 MongoDB（与 Parlant 会话库同集群不同库）

管理后台的治理数据（审批单/发布单/bad case/标注/评测报告）存 MongoDB，复用生产为 Parlant 部署的 Mongo 副本集（同集群不同库），不引入 MySQL/PG 第二套有状态服务。理由：数据形态为文档型、弱事务、少联表，正合 Mongo；基础设施复用与架构最小化（备份/监控/版本单份）。

## Status

accepted（2026-07-21，grilling 会话确认）

## Considered Options

- MySQL/PG：Java 团队最熟、联表事务强，但需新增一套 HA/备份/监控，且本场景用不上关系型优势。

## Consequences

- 生产网方案数据区描述补充"后台治理库"（同集群不同库），无新增节点。
- Java 团队需补基础 Mongo 运维常识（随 Parlant 库的使用自然消化）。
