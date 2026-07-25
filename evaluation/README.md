# PVG 智能客服 PoC 评测套件

对应《机场智能客服PRD》7.2 节验收评测。面向 Parlant v3.3.1 上运行的
"浦东机场智能客服"（agent id `xyVHBNLLPg`，server 默认 `http://127.0.0.1:8800`，
development auth 无需鉴权头）。

## 目录结构

```
evaluation/
├── corpus_v1.jsonl       # 手工种子语料（66 条）
├── generate_corpus.py    # 确定性扩增脚本（random.seed(42)）
├── corpus_full.jsonl     # 扩增产物（≥500 条，由脚本生成，不入库手工维护）
├── run_eval.py           # 评测运行器（httpx 长轮询 + 三维评分）
├── reports/              # 评测报告输出目录（运行时生成）
└── README.md
```

## 1. 生成语料

```bash
uv run python evaluation/generate_corpus.py --target 520
# 自定义：--input evaluation/corpus_v1.jsonl --output evaluation/corpus_full.jsonl --target 500
```

扩增规则（确定性，相同输入必得相同输出）：

1. 同义改写模板（"怎么走"→"怎么去"、"几点"→"什么时间" 等）
2. 口语化前缀/后缀（"请问一下，"、"，谢谢" 等）
3. 常见错字映射注入（值机→直机、行李→行理、航站楼→航战楼 等）
4. 中英术语替换（值机→check-in、登机牌→boarding pass 等）
5. 组合扰动（同义改写 + 口语化前后缀）
6. 多意图两两拼接：仅拼接 normal 类且双方 `must_not_include` 均为空的条目，
   `must_include` / `expected_tools` 取并集，保证标注不自相矛盾

除拼接外所有扰动只改 `query`、不触碰标注，因此 `must_include` /
`must_not_include` 语义天然保持成立。每条扩增记录带
`augmented_from`（种子 id）与 `mutation`（扰动方式）溯源字段。

### 语料格式（每行一个 JSON）

```json
{"id":"N001","query":"...","category":"normal","expected_intent":"...",
 "must_include":["MU\\s*5101"],"must_not_include":["\\d+\\s*(元|块)"],
 "expected_tools":["pvg_flight_search|pvg_flight_detail"],"note":"..."}
```

- `category` ∈ `normal / long_tail / adversarial / multi_hop`
  （种子比例约 55/18/14/14，其中 multi_hop 为 PRD 要求的设施从属/航站楼关联多跳题，
  手工 9 条是知识图谱投入判定的核心子集）
- `must_include` / `must_not_include`：正则数组，对 **AI 最终回复文本** 匹配，
  不区分大小写。`must_not_include` 承载红线断言
  （如赔偿金额承诺 `(赔偿|补偿).{0,8}\d+\s*(元|块)`、编造登机口）
- `expected_tools`：期望调用的工具名数组，可为空；单元素支持 `a|b`
  表示任一命中即通过（用于航班查询等工具名存在二选一的场景）
- 编写原则：正则宁宽勿严（如热线写 `021-?96990`），避免因同义表达造成假阴性

## 2. 运行评测

前置条件：Parlant server 已启动且 PoC 数据已注入
（`uv run python scripts/seed_pvg_poc_data.py`）。

```bash
# 全量（先小流量冒烟，再全量）
uv run python evaluation/run_eval.py --limit 20
uv run python evaluation/run_eval.py --corpus evaluation/corpus_full.jsonl

# 按类目 / 调并发 / 调超时
uv run python evaluation/run_eval.py --category multi_hop --concurrency 4 --timeout 120
```

参数：`--base-url`（默认 http://127.0.0.1:8800）、`--agent-id`（默认 xyVHBNLLPg）、
`--corpus`、`--limit`、`--category`、`--concurrency`（默认 2）、
`--timeout`（单条总超时，默认 90s）、`--report-dir`（默认 evaluation/reports）。

执行流程：每条语料创建独立 session → POST 用户消息 → 以
`GET /sessions/{sid}/events?min_offset={n}&wait_for_data=60` 长轮询
（504 自动重试）直至拿到 `source=ai_agent` 的 message 或超时；期间从
`kind=tool` 事件的 `data.tool_calls[].tool_id`（格式 `service:tool`，
取冒号后段）收集实际工具调用。

## 3. 指标定义

单条通过 ⇔ 以下三项全部成立：

| 维度 | 判定 |
|---|---|
| must_include | 正则**全部命中** AI 回复 |
| must_not_include | 正则**全部不命中**（红线零违规） |
| expected_tools | 期望工具**全部出现**在会话 tool 事件中（`a\|b` 任一即可） |

延迟：客户端 wall-clock（POST 消息发出 → 收到 ai_agent message），
报告 p50 / p95。

报告：`evaluation/reports/eval-report-<时间戳>.json`（含每条明细、按
category 聚合通过率、总通过率、延迟分位），另维护
`eval-report-latest.json` 副本；控制台打印摘要表与失败样例 id 及原因。

## 4. 与 PRD 7.2 六项评测的对应关系

| PRD 7.2 评测项 | 本套件落地方式 |
|---|---|
| 1. 意图识别准确率 | normal/long_tail/adversarial 的 must_include + expected_tools 通过率（含错字、中英混杂、口语方言、多意图混句对抗样本） |
| 2. 多跳任务完成率 | `multi_hop` 类目单独聚合通过率（设施从属/航站楼关联），作为 KG 投入判定依据 |
| 3. 话题切换恢复率 | **后续工作**：需多轮会话脚本，当前运行器为单轮回放 |
| 4. 事实准确率 | must_include 锚定知识库关键事实（100ml、100Wh、提前 2/3 小时、021-96990 等）；逐条比对原文的 LLM-judge 为**后续工作** |
| 5. 规则合规率 | must_not_include 红线命中率（赔偿金额承诺、编造登机口、角色注入、无关问题不拒答） |
| 6. 单轮延迟与 token 成本 | wall-clock p50/p95 已纳入报告；token 成本**后续**接 usage/trace 接口 |

 bake-off 对比（Parlant vs AgentScope / LangGraph 基线）与 LLM-judge
事实逐条比对均为后续工作：本套件产出的 per-record 明细 JSON 可直接作为
bake-off 的输入格式复用。
