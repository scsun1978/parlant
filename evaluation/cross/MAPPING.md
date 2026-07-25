# 交叉评测词汇对照与评分口径说明

本文件配套 `evaluation/cross/run_cross_a.py`（B 语料 → A 系统）与
`evaluation/cross/run_cross_b.py`（A 语料 → B 系统），记录两个系统的词汇
映射关系、评分口径差异，以及所有**近似判定**的位置与近似方式。

- 系统 A（本方）：Parlant 客服，`http://127.0.0.1:8800`，agent `xyVHBNLLPg`
- 系统 B（对方）：pvg-agent-demo implementation-v3（hermes runtime），
  `https://guest.artfox.ltd/v2-api/robot/chat`

## 1. 工具名对照

A 的工具为下划线命名（MCP 工具，见 `scripts/pvg_mcp_proxy.py`），B 的工具为
点分命名（used_tools / tool_chain 中实际产出，见 B 侧 `app/`、`services/` 源码
与其 fixtures 的 expected_tool）。

| B 工具名 | A 工具名 | 关系 |
| --- | --- | --- |
| `pvg.flight.search` | `pvg_flight_search` | 同义：航班列表搜索 |
| `pvg.flight.batch_search` | `pvg_flight_search` | 近似：B 的批量/多段搜索，A 以多次 search 实现 |
| `pvg.flight.query` | `pvg_flight_detail` | 同义：单航班实时状态/详情（A 侧由 search 后端兜底实现） |
| `pvg.flight.status` | `pvg_flight_detail` | 近似：B 源码中偶发的状态查询别名 |
| `pvg.navigation.search_poi` | `pvg_poi_search` | 同义：楼内设施/POI 检索 |
| `pvg.facility.search` | `pvg_poi_search` | 同义：B 自己的 `_TOOL_EQUIVALENTS` 即声明 facility≡search_poi |
| `pvg.navigation.plan_route` | `pvg_poi_search` | **近似**：B 是逐段路线规划，A 无路线工具，最近词汇为 POI 检索（详见 §5） |
| `pvg.navigation.create_miniprogram_link` | `pvg_poi_search` | **近似**：路线小程序卡片，归 POI/导航域 |
| `pvg.lost_found.search` | `pvg_lost_and_found_search` | 同义：拾获记录检索 |
| `pvg.lost_found.query_or_create` | `pvg_lost_and_found_search` | **近似**：B 含登记语义，A 只查拾获、明确不做线上登记 |
| `pvg.airport.search` | `pvg_airport_search` | 同义：机场字典检索 |
| `pvg.knowledge.search` | `pvg_knowledge_search` | 同义：政策/FAQ 知识库 |
| `pvg.current_datetime` | `pvg_current_datetime` | 防御性条目：B 源码未见此工具（其航班管线内部解析日期），防后续新增 |

以下 B 标签**不是真实工具**，两个方向的比对中都丢弃（与 B 评测脚本
`_POLICY_TOOL_LABELS` 的过滤行为一致）：

`pvg.baggage_or_handoff`、`pvg.handoff`、`pvg.special_assistance`、
`resource_qa`、`context.memory`、`pvg-intent-clarify`

方向差异：

- **run_cross_b（A 语料打 B）**：用上表 B→A 映射。A 语料的 `expected_tools`
  元素支持 `a|b` 任一命中（与 run_eval 一致），映射后比对。
- **run_cross_a（B 语料打 A）**：用反向映射 A→B（`TOOL_MAP_A_TO_B`，上表右列
  → 左列取最贴切的一个），再经 B 的 `_TOOL_EQUIVALENTS` 与 expected_tool
  比对。注意 expected_tool 中的 `+` 组合（如
  `pvg.flight.query+pvg.navigation.plan_route`）要求**全部**可见；policy 标签
  部分在判定前已被 B 的 `_tool_seen` 过滤。

## 2. intent 对照（run_cross_a）

B 的 intent 来自其语义解析器（`data.debug.normalized_intent` / semantic），A 没有
intent 概念。run_cross_a 用"A 实际调用的工具 + 问法关键词 + 回复文本"推断
B 的 intent 词汇，规则与依据如下（实现见 `run_cross_a.py` 头部常量与
`_infer_b_intent`）：

| B intent | A 侧推断规则 | 依据 |
| --- | --- | --- |
| `flight_status_query` | 调了 `pvg_flight_detail`（且无 POI 工具、无路线关键词） | B 中该 intent 的 expected_tool 即 `pvg.flight.query`（单航班详情） |
| `flight_schedule_search` | 调了 `pvg_flight_search` 且无更具体关键词 | `pvg.flight.search` 在 B fixtures 中的默认 intent |
| `flight_recommendation` | flight_search + 问法含 推荐/哪个好/建议/怎么选 等 | B `recommendation` 类别问法 |
| `flight_round_trip_search` | flight_search + 问法含 往返/回程/来回/返程 | B `date_time_round_trip` 类别问法 |
| `flight_route` | 航班工具 + 路线关键词（怎么走/怎么去/路线/多远…），或 航班工具+POI 工具+路线关键词 | B `flight_route` 类别为"航班+路线"复合 |
| `flight_facility` | 航班工具 + POI 工具（无路线关键词） | B `flight_facility` 类别 expected_tool=`pvg.flight.query+pvg.facility.search` |
| `navigation` | 仅 POI 工具 + 路线关键词 | B `_INTENT_EQUIVALENTS` 中 navigation 组 |
| `facility_navigation` | 仅 POI 工具、无路线关键词 | 同上等价组 |
| `lost_found_search` | 调了 `pvg_lost_and_found_search` | 失物检索，等价组含 lost_found |
| `flight_baggage` | 知识库/纯文本答复 + 行李/托运/安检/违禁 等关键词 | B `flight_baggage` 类别问法 |
| `flight_exception` | 问法含 延误/取消/改签/退票/赔偿 等 | B `flight_exception` 类别问法 |
| `channel_trust_guidance` | 问法含 小程序/航显/不一致/哪个准 等 | B `channel_trust` 类别问法 |
| `special_passenger_assistance` | 问法含 无人陪伴/轮椅/婴儿/孕妇/宠物 等 | B `special_passenger` 类别问法 |
| `staff_assistance` | 问法含 投诉/人工/工作人员 等 | B `exception_or_handoff` 类别问法 |
| `clarify` | 回复文本本身在追问航班号/日期/目的地等关键信息 | B `ambiguous_query`/`noise_or_incomplete` 类别的期望行为 |

推断不出时返回空串——B 的判定逻辑（`judge_turn`）在 observed intent 为空时
**跳过** intent 检查，不为 A 发明结论。

已知覆盖盲区（如实保留为 PARTIAL/GAP，不做额外拟合）：

- `contextual_flight_followup`（FQD `multi_turn_context` 类别，11 轮期望）：
  该 intent 依赖 B 的多轮上下文标注，A 侧无法从单轮输出可靠推断，未做
  特判；这些轮次会表现为 intent 不一致。
- `flight_transfer`、`clarify_or_flight_schedule_search`：前者靠追问行为
  （clarify 规则）间接逼近；后者被 B 的等价表覆盖（clarify 与
  flight_schedule_search 均可），A 的两种实际行为都能命中。

## 3. robot_action.code 对照

B 的动作码是跨厂商业务语义（对接文档 §7.3），A 无此概念，由推断 intent
合成（`ACTION_FROM_INTENT`）：

| code | name | 触发场景 | A 侧对应 |
| ---: | --- | --- | --- |
| 201 | present_screen | 通用屏幕呈现/闲聊/失物/渠道信任 | 无工具的纯文本答复（含知识库政策答复之外的兜底），及 lost_found、channel_trust 推断 |
| 301 | flight_info | 航班详情/列表/推荐/往返/行李答复 | 调用了航班工具，或行李类知识答复（FQD `flight_baggage` 类别期望 301） |
| 302 | route_guidance | 路线、导航、带路 | navigation / flight_route 推断（POI 工具+路线关键词，或航班工具+路线关键词） |
| 303 | facility_info | 设施查询 | facility_navigation / flight_facility 推断（POI 工具） |
| 401 | follow_up | 缺信息追问 | clarify 推断（回复在追问关键信息）、flight_transfer |
| 402 | no_result | 真实查询无结果 | **A 永不合成**：A 的工具空结果由模型组织语言答复，无法可靠识别；402 仅是期望侧的允许值 |
| 501 | staff_assistance | 人工/航司/特殊旅客/航班异常处理 | staff_assistance / flight_exception / special_passenger_assistance 推断 |

## 4. skill 对照（run_cross_a）

passenger fixtures 以 `expected_skill` 为主要期望，A 侧由推断 intent 映射
（`SKILL_FROM_INTENT`），依据 passenger fixture 的 category→expected_skill
对应关系：

| B skill | A 侧来源 |
| --- | --- |
| `pvg-flight-search` | flight_schedule_search / flight_round_trip_search / contextual_flight_followup |
| `pvg-flight-query` | flight_status_query |
| `pvg-flight-recommendation` | flight_recommendation |
| `pvg-flight-route` | flight_route |
| `pvg-flight-baggage` | flight_baggage |
| `pvg-poi-navigation` | navigation / facility_navigation / flight_facility |
| `pvg-lost-found-search` | lost_found_search |
| `pvg-intent-clarify` | clarify |
| `pvg-staff-assistance` | staff_assistance / flight_exception / special_passenger_assistance / flight_transfer |
| `pvg-channel-trust-guidance` | channel_trust_guidance |
| `pvg-emotional-support` | 问法含 害怕/紧张/焦虑/担心 等情绪词且未调航班工具（无对应 intent，单列规则） |
| `pvg-smalltalk` | 无工具纯文本回应（问候/闲聊/拒答/兜底） |

## 5. 评分口径差异

**A 口径（run_eval.py，用于 run_cross_b）**：三维布尔判定——
`must_include` 正则全命中、`must_not_include` 正则全不命中（红线）、
`expected_tools` 全部观察到（`|` 表任一）；三项全过则 passed。关注"答复文本
说了什么/没说什么 + 用了什么工具"。

**B 口径（eval_complete_robot_corpus.py，用于 run_cross_a）**：
`judge_turn` 多维检查——HTTP 信封（status/code/has_robot/mock 标记）、
robot_action.code 白名单、skill 精确相等、intent 等价表、expected_tool
可见性（`+` 组合、policy 标签过滤、`_TOOL_EQUIVALENTS`）、语义槽位
（`_slot_score`，含相对日期解析与城市/机场/航司别名归一）。无任何失分
→ PASS；有失分但有可播报文本且动作码合法 → PARTIAL；传输失败/信封非法
/动作码不合法 → GAP。用例级取多轮最差（GAP > PARTIAL > PASS）。
run_cross_a **逐行移植**了 B 的判定函数与等价表，未做改动。

## 6. 近似判定清单（重要）

以下判定是近似的，逐条说明近似在哪：

1. **A 的 intent/skill/action_code 是合成的**（§2/§3/§4）：A 没有这些概念，
   由工具调用与文本规则推断。推断错误会让对应检查失分（PARTIAL），也可能
   因规则凑巧而放过——属于跨系统评测的固有近似，规则全部公开在
   `run_cross_a.py` 头部。
2. **信封检查对 A 是合成通过的**：A 能返回文本即视为 status=200/code=0/
   has_robot=True；mock 标记检查（traceback/internal server error/demo_mock/
   "mock"）作用于 A 的答复原文。A 超时/报错 → status=0 → 传输级 GAP，
   与 B 的 transport GAP 语义对齐。
3. **expected_request_kind 检查跳过**：该检查要求 B 语义解析器的
   `candidate=True`，A 无法合成，按 B 自身逻辑（candidate 不为 True 时不查）
   自然跳过。即 one_way/round_trip/detail 等请求形态差异**不计入** A 的失分。
4. **槽位评分基于 A 的工具调用参数**：destination/departure_date/
   flight_number/airline/return_date 从 `pvg_flight_search`/`pvg_flight_detail`
   的实参合成 B 的 plan 结构后走 B 的 `_slot_score`（含 TODAY/TODAY+N 相对
   日期、城市机场别名、航司归一）。**sort_preference 必失分**（A 工具无排序
   偏好参数，fixtures 中共 12 处）；fixtures 中无 origin 槽位，未做注入。
5. **`pvg.navigation.plan_route` 在 A 无对应工具**：B 的路线类用例
   （route_guidance、flight_route 等）期望 plan_route 可见，A 只有 POI 检索，
   该检查恒失分 → 这些用例对 A 封顶 PARTIAL。这是真实能力差，如实呈现。
6. **多轮上下文**：B 的部分用例依赖请求 `context`（location_id 等），A 的
   API 无此入参，只能依靠 turns 文本中的位置表述；隐式位置类用例对 A 偏严。
   反向（run_cross_b）则总是携带固定 DEFAULT_CONTEXT（T2/3F/GATE_70）。
7. **run_cross_b 的回复文本取舍**：speech_text 优先、回退 reply/screen_text
   （title+summary）。B 的卡片/按钮等结构化内容不参与 A 的正则比对，屏幕
   专属信息可能漏判。
8. **A 语料为单轮**：corpus_v1.jsonl 无 turns 字段，run_cross_b 的多轮支持
   （同一 session_id 顺序发送）为预留能力，当前 66 条均为单轮。
9. **延迟口径**：run_cross_b 的 latency 为各轮 API wall-clock 之和；
   run_cross_a 沿用 run_eval 口径（发消息→最后内容事件，不含静默窗口）。
   两侧网络路径不同（本地 vs 公网），延迟不具备直接可比性。
