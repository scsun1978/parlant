# Parlant 深度研究报告（emcie-co/parlant v3.3.1）

- 仓库：https://github.com/emcie-co/parlant（18.2k stars，Apache-2.0，Python）
- 定位："The interaction control harness for customer-facing AI agents"——面向客户的 AI 交互控制 harness，不是通用 agent 框架
- 研究日期：2026-07-19，基于 develop 分支代码级阅读

## 一、项目概况

| 维度 | 结论 |
|---|---|
| 代码规模 | 源码 173 个 Python 文件 / ~92,700 行；测试 116 个文件 / ~64,400 行（测试/源码比 0.7，质量很好） |
| 包结构 | `core/`（领域模型+引擎）、`adapters/`（nlp/db/vector_db/tracing）、`api/`（FastAPI REST + 自带 React 聊天工作台）、`bin/`（server/client/migration CLI） |
| 关键依赖 | FastAPI、lagom（DI）、nano-vectordb/chroma/qdrant、networkx（journey 图）、jinja2（话术模板）、structlog、OpenTelemetry、fastmcp、authlib |
| LLM 提供商 | **22 个适配器**：OpenAI、Anthropic、Bedrock、Azure、Gemini、DeepSeek、LiteLLM、Ollama、**智谱 GLM、ModelScope/Qwen** 等——对国内场景友好 |
| 迭代活跃度 | 非常活跃（基本每月 1-2 版），但**破坏性变更频繁**（3.3.1 刚重命名 journey `conditions`→`triggers`、废弃 SDK 方法），升级有迁移成本 |

## 二、Guideline 机制（核心）

### 定义

最小行为单元，业务人员可直接书写：

```
GuidelineContent{ condition: "客户询问延误赔偿时",
                  action: "先致歉，然后告知其可查询延误险政策" }
```

附带 criticality（LOW/MEDIUM/HIGH）、priority、track（追踪已应用）、tags、composition_mode 等元数据。

### 运行时匹配：反直觉的"全量 + LLM 逐条判断"

- **检索阶段不做语义过滤**——拉出 agent 全部相关 guideline 直接进 LLM 匹配
- `GuidelineMatcher` 把 guideline 切成 **7 类批次**（观察型/动作型/本会话已应用/客户相关/低关键级/歧义消解/journey 节点选择），每批一次结构化 LLM 调用（JSON schema 输出），批次并行 + 重试 3 次
- 产出 `GuidelineMatch{guideline, score, rationale}`——**每条匹配都带理由，可解释**
- "已应用追踪"避免重复话术（延误致歉不会说两遍）

### 关系仲裁：纯代码、无 LLM 的确定性层

`RelationalResolver`（1568 行）处理 guideline 间关系：ENTAILMENT（蕴含）、PRIORITY（优先级压制）、DEPENDENCY（依赖 AND/OR 组）、DISAMBIGUATION（歧义消解）、OVERLAP。采用**论证理论（argumentation theory）的 reinstatement 原则**——全系统数学上最硬的一层。

### 新增规则的质量门

独立 Evaluation 服务：新增 guideline 时自动做 **coherence check（与现有规则冲突检测）**、action 提议、意图分类。

## 三、生成与控制链路（AlphaEngine）

一次用户消息的完整流程：

```
客户消息 Event 入库 → acknowledged 状态事件
 → 加载 ContextVariable / Glossary 术语 / Capabilities
 → 迭代循环：Guideline 匹配 → RelationalResolver 仲裁
      → 区分普通 vs 工具绑定 guideline → 工具推断与执行
      → 工具结果可能激活新 guideline → 再来一轮（上限 max_engine_iterations）
 → 消息生成（MessageGenerator 或 CannedResponseGenerator）
 → 写 AgentState（applied_guideline_ids、journey_paths）→ ready
```

### 防幻觉是结构性的，不是外挂 guardrail

`MessageGenerator` 内置 **revision 机制（最多 5 轮自我修订）**，每轮结构化输出：

- `factual_information_provided[]`：逐条事实 + `is_source_based_in_this_prompt`——**事实必须能溯源到 prompt 内的工具结果/上下文，否则继续修订**
- `instructions_followed / instructions_broken`：逐条 guideline 遵守情况
- `is_repeat_message`（防复读）

取第一个"全部遵守"的修订版；另有独立 ResponseAnalysisBatch 校验。这就是其 ARQ 论文（arxiv:2503.03669）的落地形态。

### Canned Response（预设话术）——客服场景最重要

四档 CompositionMode：

| 模式 | 行为 |
|---|---|
| FLUID | 完全自由生成（默认） |
| CANNED_FLUID | 优先选模板，选不到才自由生成 |
| CANNED_COMPOSITED | 必须基于模板改写 |
| **CANNED_STRICT** | **只能从预审批模板中选**，选不到发兜底话术——话术零自由发挥 |

实现四阶段：fluid 草稿 → 检索模板 → jinja2 渲染（支持客户/工具字段替换）→ LLM 选最贴合模板。模板可按 journey 作用域收窄。

## 四、工具调用

- 三种接入：本地 Python 插件（`@p.tool`）、**OpenAPI 导入（swagger 直接变工具）**、MCP
- **条件式工具供给**：工具默认不可用，仅当绑定的 guideline 被激活才暴露给 LLM——避免全量挂上下文导致误调用
- 缺参时不硬调，产出 `MissingToolData` insight 喂给生成器去**反问客户**
- 工具可通过 `ToolResult.control.mode="manual"` 直接把 session 切人工——**人工接管原生支持**

## 五、会话与状态

- Event 模型中 `HUMAN_AGENT / HUMAN_AGENT_ON_BEHALF_OF_AI_AGENT` 是一等公民事件源，转人工后 AI 自动静默
- 无状态引擎 + 持久化事件流：每轮把完整历史重新匹配，天然支持断点续聊
- 持久化：JSON 文件（默认）/ MongoDB / Snowflake；向量库 chroma/qdrant/mongo
- 并发：全异步 + 读写锁；新消息到达自动取消已过时的处理，消息生成段有不可取消保护

## 六、Glossary 与 Journey

- **Glossary**：唯一用向量检索的地方——术语按 embedding 召回，每轮多次重新加载，保证话术用词统一（"超售""非自愿降舱"口径一致）
- **Journey**：图结构多步流程，但**不直接进 prompt，而是投影成 guideline** 复用整条匹配管线；支持自适应回溯、跳步、提前退出——是"引导"而非"状态机锁死"，比硬流程引擎体验好

## 七、与 LangGraph / Dify 的本质差异

| 维度 | Parlant | LangGraph / Dify |
|---|---|---|
| 控制模型 | 声明式规则 + **每轮全量重评估**，规则随场景动态组合 | 编译期确定的图/工作流 |
| 确定性来源 | ① 每轮上下文收窄防指令稀释 ② 纯代码关系仲裁 ③ 生成后 revision 强制事实溯源 ④ canned strict 物理锁死输出空间 | 图结构本身 |
| 非线性对话 | 天然支持（插话/跳话题/反悔，下轮自动重排） | 弱，图路由易碎 |
| 牺牲 | 不适合多步自主任务；每轮多次 LLM 调用，**延迟和 token 成本显著更高**；匹配是 LLM 判断，存在理论漏匹配概率 | 反向 |

一句话：**Parlant 用 LLM 推理换流程灵活性，用多次结构化调用换行为可控性；LangGraph 用图结构换执行确定性。**

## 八、亮点

1. Guideline 心智模型与客服 SOP 完美同构，业务人员可直接维护，有 coherence check 防规则打架
2. **CANNED_STRICT 是开源界对"话术逐字合规"最完整的工程解**
3. 防幻觉是结构性的：revision 循环强制事实溯源，工具数据是唯一事实来源
4. 可解释性一流：每条规则的匹配/丢弃都有 rationale，投诉复盘、监管审计可直接取证
5. 人工接管、重复话术抑制、重入对话等客服细节极深——明显是真实客服场景打出来的设计

## 九、风险

1. **确定性是"高概率"非"数学保证"**：红线场景仍需 canned strict + 自建输出过滤双保险
2. **延迟与成本可观**：一次回复 = 多批匹配 + 工具推断 + 生成（含内部修订）+ 模板选择，高并发需实测 TTFB 与 token 账单
3. **项目年轻、API 变动快**；内部推理 prompt 全英文，中文指令遵循效果需实测
4. **生产拓扑单薄**：默认单进程 + JSON 文件存储，多副本/会话亲和性需自行验证
5. **生态薄**：无 Java SDK、无国产 IM/呼叫中心连接器

## 十、机场智能客服适配判断

| 需求 | 适配度 | 说明 |
|---|---|---|
| 航班查询 | ✅ 开箱即用 | OpenAPI 导入即成工具，绑定 guideline 条件式供给，缺参自动反问 |
| 延误政策/退改签规则 | ✅ 开箱即用 | Guideline + Glossary 术语统一 + 事实溯源 revision |
| 话术严格合规 | ✅ **核心卖点** | CANNED_STRICT + 预审批话术库 + 兜底话术 |
| 多步流程（退改签引导、投诉登记） | ✅ 开箱即用 | Journey 自适应流程，进度持久化 |
| 转人工 | ✅ 开箱即用 | 工具切 manual 模式 + HUMAN_AGENT 事件源 + AI 自动静默 |
| 中文场景 | ⚠️ 需验证 | 支持 DeepSeek/GLM/Qwen，但内部推理 prompt 英文，需实测 |
| 知识库 RAG | 🔨 要自建 | 只有 retriever 钩子（注入 transient guideline），无现成文档索引产品 |
| Java 后端集成 | 🔨 要自建 | 无 Java SDK；推荐 Parlant 独立 Python 服务 + Mongo，Java 走 REST/事件流 |
| 高可用/审计 | 🔨 部分自建 | OTEL/健康端点/匹配 rationale 现成；多副本与话术版本治理需自己设计 |

## 总体结论

Parlant 是目前开源选项中与"高合规客服"需求契合度最高的一档——它把 liteflow-react-agent 方案的"流程编排"换成了"行为约束"，恰好命中机场场景**"不怕流程不自由、就怕话术乱说"**的核心矛盾。

**建议验证路径**：DeepSeek/Qwen 适配器 + 30~50 条真实客服 guideline + canned strict 话术库做 2 周 POC，重点压测：
1. 单轮端到端延迟（多批匹配 + 修订循环）
2. 中文匹配准确率
3. 大 guideline 量下的 token 成本

三项达标即可认定生产可行。
