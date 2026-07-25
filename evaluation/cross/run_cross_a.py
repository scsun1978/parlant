"""交叉评测（方向 A）：B 的 621 例语料打系统 A（本方 Parlant 客服）。

用途：与 B（pvg-agent-demo implementation-v3，hermes runtime）做 bake-off。
读取 B 侧 4 个 fixtures（共 621 例 / 662 轮），逐例在 A 上创建独立 session、
按 turns 顺序多轮回放（复用 evaluation/run_eval.py 的会话与长轮询逻辑），
然后**忠实复刻** B 侧 scripts/eval_complete_robot_corpus.py 的评分口径
（PASS / PARTIAL / GAP，含 _INTENT_EQUIVALENTS / _TOOL_EQUIVALENTS /
_POLICY_TOOL_LABELS / 槽位评分）判定。

跨系统适配（近似点详见 evaluation/cross/MAPPING.md）：
  - A 没有 B 的 intent/skill/action_code/semantic 概念，这些字段由
    "A 的回复文本 + 实际调用的工具（含参数）" 经本文件头部映射表推断合成，
    再送入与 B 完全相同的 judge_turn 判定；
  - expected_tool 中的 B 工具名经 _TOOL_EQUIVALENTS 等价表与 A 工具名比对
    （A→B 工具名映射表 TOOL_MAP_A_TO_B）；
  - expected_slots 与 A 工具调用参数合成的 plan 比对（复用 B 的
    _slot_score / _flatten_plan_slots / 城市机场别名表 / 航司规范化）；
  - expected_request_kind 依赖 B 语义解析器的 candidate 标记，A 侧无法
    合成，该检查按 B 自身逻辑自然跳过（candidate 不为 True 时不检查）。

用法：
  uv run python evaluation/cross/run_cross_a.py --limit 20
  uv run python evaluation/cross/run_cross_a.py --category flight_detail --priority P0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

# 复用 A 侧评测运行器的轮询常量、事件解析与统计函数（含 preamble 与
# 多消息拼接逻辑）；run_eval.py 无包结构，直接把 evaluation/ 加入 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_eval  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")

# B 侧 fixtures 默认目录（implementation-v3 仓库内）
DEFAULT_FIXTURES_DIR = (
    "/Users/shengchun.sun/Library/CloudStorage/OneDrive-个人/MyCloud/Code/"
    "pvg-agent-demo/implementation-v3/pvg-agent-demo/tests/fixtures"
)
# 与 B 评测脚本 DEFAULT_FIXTURES 一致（顺序即加载顺序）
DEFAULT_FIXTURES = (
    "flight_query_diverse_questions.json",
    "passenger_service_opening_utterances.json",
    "multi_flight_region_time_questions.json",
    "flight_semantic_v2_questions.json",
)

# ---------------------------------------------------------------------------
# A → B 词汇映射表（每个映射的依据见各行注释；更完整说明见 MAPPING.md）
# ---------------------------------------------------------------------------

# A 工具名 → B 工具名。依据 B 侧 app/ services/ 源码中 tool_chain/used_tools
# 的实际产出名与 fixtures 中 expected_tool 用词。
TOOL_MAP_A_TO_B: dict[str, str] = {
    # 航班列表搜索：两侧均为"按日期/方向/城市/航司检索航班"的 MCP 工具
    "pvg_flight_search": "pvg.flight.search",
    # 单航班详情：B 的 pvg.flight.query 即单航班实时状态查询；A 的
    # pvg_flight_detail 由 pvg_flight_search 后端兜底实现（scripts/pvg_mcp_proxy.py:126）
    "pvg_flight_detail": "pvg.flight.query",
    # 机场字典检索：两侧同名同义
    "pvg_airport_search": "pvg.airport.search",
    # 楼内设施/POI 检索：B 的 _TOOL_EQUIVALENTS 将 pvg.facility.search 与
    # pvg.navigation.search_poi 视为等价，POI 检索即设施检索
    "pvg_poi_search": "pvg.navigation.search_poi",
    # 失物招领检索：两侧均为"查询拾获记录"（B 另有 query_or_create 含登记，A 无）
    "pvg_lost_and_found_search": "pvg.lost_found.search",
    # 政策/FAQ 知识库：B 工具网关中存在 pvg.knowledge.search（源码可见），
    # 但 B fixtures 从不把它列为 expected_tool（政策类用例期望的是
    # resource_qa/pvg.handoff 等 policy 标签，判定前已被过滤），因此该映射
    # 只影响报告可读性，不影响命中
    "pvg_knowledge_search": "pvg.knowledge.search",
    # 当前时间：B 无同名工具（其航班管线内部解析日期），直译名仅作展示，
    # 不会参与 expected_tool 命中
    "pvg_current_datetime": "pvg.current_datetime",
}

# B intent ← A 侧推断所用的文本规则（仅工具优先规则之外的补充）。
# 依据：B fixtures 中各 category 的问法关键词与 expected_primary_intent 的对应。
_ROUND_TRIP_RE = re.compile(r"往返|回程|来回|返程|round\s?trip", re.IGNORECASE)
_RECOMMEND_RE = re.compile(r"推荐|哪个好|哪班好|建议|怎么选|选哪|比较合适|划算")
_ROUTE_RE = re.compile(r"怎么走|怎么去|路线|带路|导航|多远|多久到|步行|坐.{0,3}(车|地铁|捷运)|打车")
_BAGGAGE_RE = re.compile(r"行李|托运|超重|超规|充电宝|液体|安检|违禁|打火机|喷雾|能带|随身携带|手提")
_SPECIAL_PASSENGER_RE = re.compile(r"无人陪伴|无陪|轮椅|婴儿|孕妇|老人|宠物|儿童单独|病患|担架|聋哑|盲人")
_EXCEPTION_RE = re.compile(r"延误|取消|备降|返航|改签|退票|晚点|赔偿|补偿")
_CHANNEL_TRUST_RE = re.compile(r"小程序|航显|显示屏|不一致|哪个准|以谁为准|不一样")
_STAFF_RE = re.compile(r"投诉|人工|客服|工作人员|值班|负责人")
_EMOTION_RE = re.compile(r"害怕|紧张|焦虑|担心|难受|委屈|生气|哭|吓|恐惧|不安|着急")
_CLARIFY_RE = re.compile(r"(请问|请提供|麻烦提供|请告知|告诉我).{0,12}(航班号|航班|日期|哪天|出发地|目的地|城市|位置|几号|哪个)")

# B intent → robot_action.code。依据：B fixtures 中各 intent 对应类别的
# expected_robot_action_code 分布（见 MAPPING.md 表格）与对接文档 §7.3。
ACTION_FROM_INTENT: dict[str, int] = {
    "flight_schedule_search": 301,     # destination_search 等类别 action=301
    "flight_status_query": 301,        # flight_detail/flight_dynamic 类别 action=301
    "flight_recommendation": 301,      # recommendation 类别 action=301
    "flight_round_trip_search": 301,   # date_time_round_trip 类别 action=301
    "contextual_flight_followup": 301, # multi_turn_context 类别 action=301
    "flight_baggage": 301,             # flight_baggage 类别 action=301（行李答复挂在航班卡片）
    "flight_route": 302,               # flight_route 类别 action=302
    "navigation": 302,                 # route_flight_boundary 中 navigation action=302
    "flight_facility": 303,            # flight_facility 类别 action=303
    "facility_navigation": 303,        # 设施定位：对接文档 §7.3 code 303
    "clarify": 401,                    # clarify_ambiguity_unsupported 类别 action=401
    "flight_transfer": 401,            # multi_leg_transfer_limit 类别 action=401（追问联程信息）
    "lost_found_search": 201,          # passenger lost_found 类别 action=201
    "channel_trust_guidance": 201,     # channel_trust 类别 action=201
    "flight_exception": 501,           # flight_exception 类别 action=501
    "special_passenger_assistance": 501,  # special_passenger 类别 action=501
    "staff_assistance": 501,           # exception_or_handoff 类别 action=501
}

# B intent → data.skill。依据：passenger_service_opening_utterances.json 中
# category → expected_skill 的对应关系（如 flight_search→pvg-flight-search、
# facility_search→pvg-poi-navigation、lost_found→pvg-lost-found-search）。
SKILL_FROM_INTENT: dict[str, str] = {
    "flight_schedule_search": "pvg-flight-search",
    "flight_status_query": "pvg-flight-query",
    "flight_recommendation": "pvg-flight-recommendation",
    "flight_round_trip_search": "pvg-flight-search",
    "contextual_flight_followup": "pvg-flight-search",
    "flight_route": "pvg-flight-route",
    "flight_baggage": "pvg-flight-baggage",
    "navigation": "pvg-poi-navigation",
    "facility_navigation": "pvg-poi-navigation",
    "flight_facility": "pvg-poi-navigation",
    "lost_found_search": "pvg-lost-found-search",
    "clarify": "pvg-intent-clarify",
    "flight_transfer": "pvg-staff-assistance",
    "flight_exception": "pvg-staff-assistance",
    "special_passenger_assistance": "pvg-staff-assistance",
    "staff_assistance": "pvg-staff-assistance",
    "channel_trust_guidance": "pvg-channel-trust-guidance",
}

# ---------------------------------------------------------------------------
# 以下 _INTENT_EQUIVALENTS / _TOOL_EQUIVALENTS / _POLICY_TOOL_LABELS 及
# judge_turn 系列函数忠实移植自 B 侧
# scripts/eval_complete_robot_corpus.py（判定逻辑与等价表不得改动）；
# CITY_AIRPORT_ALIASES / AIRLINE_ALIASES / canonical_airline_name 移植自
# B 侧 services/mcp_gateway/flight_search_normalization.py。
# ---------------------------------------------------------------------------

_INTENT_EQUIVALENTS = {
    "navigation": {"navigation", "poi_route"},
    "facility_navigation": {"facility_navigation", "navigation", "poi_route"},
    "flight_facility": {"flight_facility", "facility_search", "facility_navigation"},
    "flight_baggage": {"flight_baggage", "baggage_guidance"},
    "flight_transfer": {"flight_transfer"},
    "lost_found_search": {"lost_found_search", "lost_found"},
    "staff_assistance": {"staff_assistance", "complaint_handoff"},
    # The answer composer exposes flight_gate for a verified flight-detail card.
    "flight_status_query": {"flight_status_query", "flight_gate"},
    "clarify_or_flight_schedule_search": {"clarify", "flight_schedule_search"},
}
_TOOL_EQUIVALENTS = {"pvg.facility.search": {"pvg.facility.search", "pvg.navigation.search_poi"}}
_POLICY_TOOL_LABELS = frozenset({"pvg.baggage_or_handoff", "pvg.handoff", "pvg.special_assistance", "resource_qa", "context.memory"})

CITY_AIRPORT_ALIASES: dict[str, tuple[str, ...]] = {
    "北京": ("北京", "首都", "大兴", "PEK", "PKX"),
    "上海": ("上海", "浦东", "虹桥", "PVG", "SHA"),
    "广州": ("广州", "白云", "CAN"),
    "深圳": ("深圳", "宝安", "SZX"),
    "成都": ("成都", "天府", "双流", "TFU", "CTU"),
    "重庆": ("重庆", "江北", "CKG"),
    "西安": ("西安", "咸阳", "XIY"),
    "杭州": ("杭州", "萧山", "HGH"),
    "南京": ("南京", "禄口", "NKG"),
    "厦门": ("厦门", "高崎", "XMN"),
    "青岛": ("青岛", "胶东", "TAO"),
    "昆明": ("昆明", "长水", "KMG"),
    "武汉": ("武汉", "天河", "WUH"),
    "长沙": ("长沙", "黄花", "CSX"),
    "郑州": ("郑州", "新郑", "CGO"),
    "天津": ("天津", "滨海", "TSN"),
    "大连": ("大连", "周水子", "DLC"),
    "沈阳": ("沈阳", "桃仙", "SHE"),
    "哈尔滨": ("哈尔滨", "太平", "HRB"),
    "乌鲁木齐": ("乌鲁木齐", "地窝堡", "URC"),
    "海口": ("海口", "美兰", "HAK"),
    "三亚": ("三亚", "凤凰", "SYX"),
    "福州": ("福州", "长乐", "FOC"),
    "贵阳": ("贵阳", "龙洞堡", "KWE"),
    "南宁": ("南宁", "吴圩", "NNG"),
    "兰州": ("兰州", "中川", "LHW"),
    "银川": ("银川", "河东", "INC"),
    "呼和浩特": ("呼和浩特", "白塔", "HET"),
    "济南": ("济南", "遥墙", "TNA"),
    "石家庄": ("石家庄", "正定", "SJW"),
    "合肥": ("合肥", "新桥", "HFE"),
    "南昌": ("南昌", "昌北", "KHN"),
    "太原": ("太原", "武宿", "TYN"),
    "宁波": ("宁波", "栎社", "NGB"),
    "温州": ("温州", "龙湾", "WNZ"),
    "珠海": ("珠海", "金湾", "ZUH"),
    "拉萨": ("拉萨", "贡嘎", "LXA"),
    "西宁": ("西宁", "曹家堡", "XNN"),
    "喀什": ("喀什", "KHG"),
    "桂林": ("桂林", "两江", "KWL"),
    "泉州": ("泉州", "晋江", "JJN"),
    "丽江": ("丽江", "三义", "LJG"),
    "东京": ("东京", "羽田", "成田", "HND", "NRT"),
    "大阪": ("大阪", "关西", "KIX"),
    "楠迪": ("楠迪", "南迪", "Nadi", "NAN"),
    "苏瓦": ("苏瓦", "Suva", "SUV"),
    "亚的斯亚贝巴": ("亚的斯亚贝巴", "ADD"),
    "伊斯坦布尔": ("伊斯坦布尔", "IST"),
    "伦敦": ("伦敦", "盖特威克", "希思罗", "LGW", "LHR"),
    "吉隆坡": ("吉隆坡", "KUL"),
    "多哈": ("多哈", "哈马德", "DOH"),
    "奥克兰": ("奥克兰", "AKL"),
    "威尼斯": ("威尼斯", "泰塞拉", "VCE"),
    "巴塞罗那": ("巴塞罗那", "巴塞罗纳", "BCN"),
    "巴黎": ("巴黎", "夏尔戴高乐", "戴高乐", "CDG"),
    "悉尼": ("悉尼", "SYD"),
    "墨尔本": ("墨尔本", "MEL"),
    "新加坡": ("新加坡", "樟宜", "SIN"),
    "日内瓦": ("日内瓦", "GVA"),
    "苏黎世": ("苏黎世", "ZRH"),
    "曼彻斯特": ("曼彻斯特", "MAN"),
    "曼谷": ("曼谷", "素万那普", "BKK"),
    "法兰克福": ("法兰克福", "FRA"),
    "柏林": ("柏林", "BER"),
    "慕尼黑": ("慕尼黑", "MUC"),
    "米兰": ("米兰", "马尔彭萨", "MXP"),
    "罗马": ("罗马", "菲乌米奇诺", "FCO"),
    "迪拜": ("迪拜", "DXB"),
    "首尔": ("首尔", "韩国首尔", "仁川", "ICN"),
    "马尼拉": ("马尼拉", "尼诺阿基诺", "MNL"),
    "马德里": ("马德里", "巴拉哈斯", "MAD"),
    "洛杉矶": ("洛杉矶", "LAX"),
    "纽约": ("纽约", "JFK", "纽瓦克", "EWR"),
    "旧金山": ("旧金山", "SFO"),
    "温哥华": ("温哥华", "YVR"),
    "阿姆斯特丹": ("阿姆斯特丹", "AMS"),
    "布鲁塞尔": ("布鲁塞尔", "BRU"),
    "维也纳": ("维也纳", "VIE"),
    "赫尔辛基": ("赫尔辛基", "HEL"),
    "哥本哈根": ("哥本哈根", "CPH"),
    "莫斯科": ("莫斯科", "SVO", "DME"),
    "胡志明": ("胡志明", "SGN"),
    "河内": ("河内", "HAN"),
    "金边": ("金边", "PNH"),
    "雅加达": ("雅加达", "CGK"),
}

AIRLINE_ALIASES: dict[str, tuple[str, ...]] = {
    "中国国际航空公司": ("中国国际航空公司", "中国国际航空", "中国国航", "国航", "CA"),
    "中国东方航空公司": ("中国东方航空公司", "中国东方航空", "东方航空", "东航", "MU", "FM"),
    "中国南方航空公司": ("中国南方航空公司", "中国南方航空", "南方航空", "南航", "CZ"),
    "上海航空公司": ("上海航空公司", "上海航空", "上航", "FM"),
    "海南航空公司": ("海南航空公司", "海南航空", "海航", "HU"),
    "春秋航空公司": ("春秋航空公司", "春秋航空", "春秋", "9C"),
    "吉祥航空公司": ("吉祥航空公司", "吉祥航空", "吉祥", "HO"),
    "四川航空公司": ("四川航空公司", "四川航空", "川航", "四川", "3U"),
    "厦门航空公司": ("厦门航空公司", "厦门航空", "厦航", "MF"),
    "中国联合航空公司": ("中国联合航空公司", "中国联合航空", "联合航空", "中联航", "KN"),
    "深圳航空公司": ("深圳航空公司", "深圳航空", "深航", "ZH"),
    "山东航空公司": ("山东航空公司", "山东航空", "山航", "SC"),
}

_AIRLINE_ALIAS_TO_CANONICAL = {
    re.sub(r"\s+", "", str(alias or "").strip()).upper(): canonical
    for canonical, aliases in AIRLINE_ALIASES.items()
    for alias in aliases
}


def canonical_airline_name(value: str) -> str:
    """移植自 B 侧 flight_search_normalization.canonical_airline_name。"""

    normalized = re.sub(r"\s+", "", str(value or "").strip()).upper()
    return _AIRLINE_ALIAS_TO_CANONICAL.get(normalized, str(value or "").strip())


# ---------------------------------------------------------------------------
# B 侧 load_cases / judge_turn 系列函数移植（逻辑保持一致，仅把 fixtures
# 目录改为参数、去掉对本仓库以外的依赖）
# ---------------------------------------------------------------------------


def load_cases(
    fixtures_dir: Path,
    fixture_names: tuple[str, ...] = DEFAULT_FIXTURES,
) -> list[dict[str, Any]]:
    """读取 B 侧 fixtures，结构与 B 评测脚本 load_cases 输出一致。"""

    cases: list[dict[str, Any]] = []
    for fixture_name in fixture_names:
        dataset = json.loads((fixtures_dir / fixture_name).read_text(encoding="utf-8"))
        for raw in dataset:
            case = dict(raw)
            case["fixture"] = fixture_name
            if "turns" not in case:
                case["turns"] = [case.get("question", "")]
            if "expectations" not in case:
                expected = {
                    "expected_primary_intent": case.get("expected_primary_intent", ""),
                    "expected_tool": case.get("expected_tool", ""),
                    "expected_robot_action_code": case.get("expected_robot_action_code"),
                    "expected_skill": case.get("expected_skill", ""),
                    "allowed_robot_action_codes": case.get("allowed_robot_action_codes", []),
                    "expected_slots": {},
                }
                case["expectations"] = [dict(expected) for _ in case["turns"]]
            else:
                case["expectations"] = [
                    _flatten_arbitration_expectation(item)
                    for item in case.get("expectations", [])
                ]
            cases.append(case)
    return cases


def _flatten_arbitration_expectation(expectation: dict[str, Any]) -> dict[str, Any]:
    """Expose nested V2 contract fields to the live evaluator.（B 原文）"""

    if not isinstance(expectation, dict) or "arbitration" not in expectation:
        return dict(expectation or {})
    arbitration = expectation.get("arbitration") if isinstance(expectation.get("arbitration"), dict) else {}
    downstream = expectation.get("downstream") if isinstance(expectation.get("downstream"), dict) else {}
    robot = expectation.get("robot") if isinstance(expectation.get("robot"), dict) else {}
    tools = [str(tool) for tool in downstream.get("required_tools", []) if tool]
    allowed_codes = [code for code in robot.get("allowed_action_codes", []) if isinstance(code, int)]
    flattened = {
        "expected_primary_intent": arbitration.get("intent") or downstream.get("intent") or "",
        "expected_tool": "+".join(tools),
        "expected_robot_action_code": allowed_codes[0] if allowed_codes else None,
        "allowed_robot_action_codes": allowed_codes,
        "expected_arbitration_decision": arbitration.get("decision", ""),
        "expected_arbitration_action": arbitration.get("workflow_action", ""),
        "expected_entities": arbitration.get("expected_entities", {}),
    }
    flattened.update({key: value for key, value in expectation.items() if key not in {"arbitration", "downstream", "robot"}})
    return flattened


def _semantic_observation_only(semantic: dict[str, Any]) -> bool:
    """Return true when semantic output was recorded but did not control execution.（B 原文）"""

    return (
        semantic.get("mode") in {"shadow", "canary"}
        and semantic.get("selected") is False
        and bool(semantic.get("semantic_intent"))
    )


def judge_turn(expected: dict[str, Any], status: int, raw: str, actual: dict[str, Any]) -> tuple[str, list[str], int, int]:
    """忠实移植自 B 侧 eval_complete_robot_corpus.judge_turn。

    actual 由 _build_actual 按 B 的 extract_actual 输出结构合成。
    """

    reasons: list[str] = []
    lower = raw.lower()
    if status != 200:
        detail = actual.get("error_message") or "unparseable response"
        return "GAP", [f"HTTP {status}: {detail}"], 0, 0
    if actual.get("code") != 0 or not actual.get("has_robot"):
        return "GAP", ["invalid Robot API envelope"], 0, 0
    if any(marker in lower for marker in ("traceback", "internal server error", "demo_mock", '"mock"')):
        return "GAP", ["internal/mock marker visible"], 0, 0

    semantic = actual.get("semantic") or {}
    shadow_semantic = _semantic_observation_only(semantic)
    allowed_actions = set(expected.get("allowed_robot_action_codes") or [])
    expected_action_code = expected.get("expected_robot_action_code")
    if expected_action_code is not None:
        allowed_actions.add(expected_action_code)
    if expected_action_code == 301:
        allowed_actions.add(402)  # A real, empty MCP result is valid but partial for the scenario.
    if not shadow_semantic and allowed_actions and actual.get("action_code") not in allowed_actions:
        reasons.append(f"action {actual.get('action_code')} not in {sorted(allowed_actions)}")
    elif not shadow_semantic and expected_action_code == 301 and actual.get("action_code") == 402:
        reasons.append("real MCP returned no result")

    expected_skill = str(expected.get("expected_skill") or "")
    if expected_skill and not shadow_semantic and actual.get("skill") != expected_skill:
        reasons.append(f"skill {actual.get('skill')} != {expected_skill}")
    expected_decision = str(expected.get("expected_arbitration_decision") or "")
    arbitration = actual.get("domain_arbitration") if isinstance(actual.get("domain_arbitration"), dict) else {}
    if expected_decision and arbitration.get("decision") and arbitration.get("decision") != expected_decision:
        reasons.append(f"arbitration decision {arbitration.get('decision')} != {expected_decision}")
    expected_arbitration_action = str(expected.get("expected_arbitration_action") or "")
    actual_initial_action = str(arbitration.get("initial_action") or arbitration.get("arbitration_action") or "")
    if expected_arbitration_action and actual_initial_action and actual_initial_action != expected_arbitration_action:
        reasons.append(f"arbitration action {actual_initial_action} != {expected_arbitration_action}")
    expected_intent = str(expected.get("expected_primary_intent") or "")
    observed_intent = semantic.get("semantic_intent") if shadow_semantic else actual.get("intent")
    if expected_intent and observed_intent and not _intent_matches(expected_intent, observed_intent):
        reasons.append(f"intent {observed_intent} != {expected_intent}")

    expected_kind = str(expected.get("expected_request_kind") or "")
    observed_kind = semantic.get("selected_request_kind") or semantic.get("request_kind")
    if expected_kind and semantic.get("candidate") is True and observed_kind != expected_kind:
        reasons.append(f"request_kind {observed_kind} != {expected_kind}")
    expected_tool = str(expected.get("expected_tool") or "")
    tool_seen = _tool_seen(expected_tool, actual) if expected_tool and not shadow_semantic else None
    if tool_seen is False:
        reasons.append(f"tool {expected_tool} not visible")

    score_semantic_slots = semantic.get("candidate") is not False
    slot_correct, slot_total = _slot_score(
        expected.get("expected_slots") or {} if score_semantic_slots else {},
        semantic.get("plan") or {},
    )
    if slot_correct != slot_total:
        reasons.append(f"semantic slots {slot_correct}/{slot_total}")
    if not reasons:
        return "PASS", [], slot_correct, slot_total
    if actual.get("speech_text") and actual.get("action_code") in {201, 301, 302, 303, 401, 402, 501}:
        return "PARTIAL", reasons, slot_correct, slot_total
    return "GAP", reasons, slot_correct, slot_total


def _tool_seen(expected_tool: str, actual: dict[str, Any]) -> bool | None:
    """移植自 B 侧（expected_tool 支持 "+" 组合与等价表，policy 标签过滤）。"""

    values: list[str] = []
    for item in [*(actual.get("used_tools") or []), *(actual.get("tool_chain") or [])]:
        if isinstance(item, str):
            values.append(item)
        elif isinstance(item, dict):
            values.extend(str(item.get(key) or "") for key in ("tool", "tool_name", "name"))
    values = [value for value in values if value]
    if not values:
        return None
    expected_tools = [tool.strip() for tool in expected_tool.split("+") if tool.strip() and tool.strip() not in _POLICY_TOOL_LABELS]
    if not expected_tools:
        return None
    return all(any(candidate in values for candidate in _TOOL_EQUIVALENTS.get(tool, {tool})) for tool in expected_tools)


def _intent_matches(expected: str, actual: str) -> bool:
    """移植自 B 侧（等价表匹配）。"""

    normalized_expected = str(expected or "").strip()
    normalized_actual = str(actual or "").strip()
    return normalized_actual in _INTENT_EQUIVALENTS.get(normalized_expected, {normalized_expected})


def _slot_score(expected: dict[str, Any], plan: dict[str, Any]) -> tuple[int, int]:
    """移植自 B 侧。"""

    if not expected or not plan:
        return 0, len(expected)
    actual = _flatten_plan_slots(plan)
    correct = 0
    for key, value in expected.items():
        expected_value = _resolve_relative_date(str(value))
        values = actual.get(key, [])
        if any(_slot_values_match(key, expected_value, actual_value) for actual_value in values):
            correct += 1
    return correct, len(expected)


def _slot_values_match(key: str, expected: str, actual: str) -> bool:
    """移植自 B 侧。"""

    if expected == actual:
        return True
    if key == "airline":
        return canonical_airline_name(expected) == canonical_airline_name(actual)
    if key not in {"origin", "destination"}:
        return False
    if re.fullmatch(r"[A-Z]{3}", expected.upper()):
        return bool(re.search(rf"(?<![A-Z]){re.escape(expected.upper())}(?![A-Z])", actual.upper()))
    return _city_for_alias(expected) != "" and _city_for_alias(expected) == _city_for_alias(actual)


def _city_for_alias(value: str) -> str:
    """移植自 B 侧。"""

    raw = str(value or "").strip()
    upper = raw.upper()
    for city, aliases in CITY_AIRPORT_ALIASES.items():
        if raw == city or raw in aliases:
            return city
        for alias in aliases:
            alias_text = str(alias)
            if re.fullmatch(r"[A-Za-z]{3}", alias_text):
                if re.search(rf"(?<![A-Z]){re.escape(alias_text.upper())}(?![A-Z])", upper):
                    return city
            elif alias_text and alias_text in raw:
                return city
    return ""


def _flatten_plan_slots(plan: dict[str, Any]) -> dict[str, list[str]]:
    """移植自 B 侧。"""

    values: dict[str, list[str]] = defaultdict(list)
    flight = plan.get("flight_number") if isinstance(plan.get("flight_number"), dict) else {}
    if flight.get("value"):
        values["flight_number"].append(str(flight["value"]).upper())
    airline = plan.get("airline") if isinstance(plan.get("airline"), dict) else {}
    if airline.get("value"):
        values["airline"].append(str(airline["value"]))
    preferences = plan.get("preferences") if isinstance(plan.get("preferences"), dict) else {}
    for key, raw in preferences.items():
        slot = raw if isinstance(raw, dict) else {}
        if slot.get("value"):
            value = str(slot["value"])
            values[str(key)].append(value)
            preference = _sort_preference_from_slot(str(key), value)
            if preference:
                values["sort_preference"].append(preference)
    for index, leg in enumerate(plan.get("legs") or []):
        if not isinstance(leg, dict):
            continue
        for field in ("origin", "destination", "departure_date", "return_date"):
            slot = leg.get(field) if isinstance(leg.get(field), dict) else {}
            if slot.get("value"):
                values[field].append(str(slot["value"]))
                if index > 0 and field == "departure_date":
                    values["return_date"].append(str(slot["value"]))
    return values


def _sort_preference_from_slot(key: str, value: str) -> str:
    """移植自 B 侧。"""

    normalized_key = key.strip().lower()
    normalized_value = value.strip().lower()
    aliases = {
        "earliest": "earliest",
        "earliest_departure": "earliest",
        "latest": "latest",
        "latest_departure": "latest",
        "next": "next",
        "next_departure": "next",
    }
    if normalized_key in aliases and normalized_value in {"true", "1", "yes", "是", aliases[normalized_key]}:
        return aliases[normalized_key]
    if normalized_value in {"earliest", "latest", "next"}:
        return normalized_value
    return ""


def _resolve_relative_date(value: str) -> str:
    """移植自 B 侧（TODAY / TODAY+N 按 Asia/Shanghai 解析）。"""

    today = datetime.now(SHANGHAI).date()
    if value == "TODAY":
        return today.isoformat()
    if value.startswith("TODAY+"):
        try:
            return (today + timedelta(days=int(value.split("+", 1)[1]))).isoformat()
        except ValueError:
            return value
    return value


# ---------------------------------------------------------------------------
# A 侧执行与 B 词汇合成
# ---------------------------------------------------------------------------


@dataclass
class ATurnResult:
    """A 侧单轮执行结果：回复文本 + 工具调用（含参数）+ 延迟/错误。"""

    reply: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    latency_s: float | None = None
    error: str | None = None


def _collect_tool_calls(events: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """从事件列表收集 (工具名, 调用参数)；参数供 B 槽位评分合成 plan 使用。"""

    calls: list[tuple[str, dict[str, Any]]] = []
    for event in events:
        if event.get("kind") != "tool":
            continue
        data = event.get("data") or {}
        for tool_call in data.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            name = run_eval._tool_name_from_call(tool_call)
            arguments = tool_call.get("arguments")
            if name:
                calls.append((name, arguments if isinstance(arguments, dict) else {}))
    return calls


async def _run_turn(
    client: httpx.AsyncClient,
    session_id: str,
    text: str,
    deadline: float,
) -> ATurnResult:
    """在既有 session 内执行单轮（与 run_eval._run_one 相同的长轮询逻辑）。

    与 run_eval 的差异仅在于：session 由用例级创建（多轮共用）、且保留
    工具调用参数。preamble 与多消息拼接语义与 run_eval 完全一致。
    """

    result = ATurnResult()
    started = time.monotonic()
    try:
        resp = await client.post(
            f"/sessions/{session_id}/events",
            json={"kind": "message", "source": "customer", "message": text},
        )
        resp.raise_for_status()
        min_offset = int(resp.json()["offset"]) + 1

        ai_msgs: list[str] = []
        last_progress = time.monotonic()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if ai_msgs and time.monotonic() - last_progress >= run_eval.QUIET_SECONDS:
                break
            wait = max(1, min(run_eval.POLL_WAIT_SECONDS, math.ceil(remaining)))
            resp = await client.get(
                f"/sessions/{session_id}/events",
                params={"min_offset": min_offset, "wait_for_data": wait},
                timeout=wait + 15,
            )
            if resp.status_code == 504:
                continue
            resp.raise_for_status()
            events = resp.json()
            if events:
                last_progress = time.monotonic()
                min_offset = max(int(e["offset"]) for e in events) + 1
            result.tool_calls.extend(_collect_tool_calls(events))
            ai_msgs.extend(run_eval._find_ai_messages(events))

        result.latency_s = (last_progress if ai_msgs else time.monotonic()) - started
        if not ai_msgs:
            result.error = "timeout: 未收到 ai_agent 回复"
            return result
        result.reply = "\n".join(ai_msgs)
        return result
    except (httpx.HTTPError, KeyError, ValueError) as e:
        result.error = f"{type(e).__name__}: {e}"
        result.latency_s = time.monotonic() - started
        return result


def _infer_b_intent(query: str, reply: str, a_tools: set[str]) -> str:
    """把 A 的"回复文本 + 调用工具"映射为 B 的 intent 词汇。

    规则依据：B fixtures 中 expected_primary_intent 与各 category 问法/
    expected_tool 的对应关系（见文件头部正则与 MAPPING.md）。推断不出时
    返回 ""——B 的判定逻辑在 observed intent 为空时跳过 intent 检查，
    不为 A 发明结论。
    """

    # 1) 工具优先：航班类工具是最强的意图信号
    if "pvg_flight_detail" in a_tools or "pvg_flight_search" in a_tools:
        if "pvg_poi_search" in a_tools:
            # 航班 + 设施/路线复合：对应 B 的 flight_route / flight_facility 类别
            return "flight_route" if _ROUTE_RE.search(query) else "flight_facility"
        if "pvg_flight_detail" in a_tools:
            # 单航班详情：B 中 pvg.flight.query 对应 flight_status_query
            if _ROUTE_RE.search(query):
                return "flight_route"
            return "flight_status_query"
        # 航班列表搜索的子类按问法关键词细分（依据 B 各类别问法）
        if _ROUND_TRIP_RE.search(query):
            return "flight_round_trip_search"
        if _RECOMMEND_RE.search(query):
            return "flight_recommendation"
        if _ROUTE_RE.search(query):
            return "flight_route"
        return "flight_schedule_search"
    if "pvg_lost_and_found_search" in a_tools:
        return "lost_found_search"
    if "pvg_poi_search" in a_tools:
        return "navigation" if _ROUTE_RE.search(query) else "facility_navigation"
    # 2) 情绪安抚无对应 B intent：交给 skill 规则处理，intent 留空
    if _EMOTION_RE.search(query):
        return ""
    # 3) 知识库/纯文本答复按问法关键词归类（依据 FQD 各类别问法）
    if _SPECIAL_PASSENGER_RE.search(query):
        return "special_passenger_assistance"
    if _BAGGAGE_RE.search(query):
        return "flight_baggage"
    if _EXCEPTION_RE.search(query):
        return "flight_exception"
    if _CHANNEL_TRUST_RE.search(query):
        return "channel_trust_guidance"
    if _STAFF_RE.search(query):
        return "staff_assistance"
    # 4) 回复本身在追问关键信息 → clarify（ambiguous/noise 类用例）
    if _CLARIFY_RE.search(reply):
        return "clarify"
    return ""


def _infer_b_skill(query: str, intent: str, a_tools: set[str], reply: str) -> str:
    """由推断的 intent 映射 B 的 data.skill 词汇。

    依据：passenger fixtures 的 category→expected_skill 对应。情绪安抚类
    无独立 intent，但 B 有 pvg-emotional-support skill，单列规则。
    """

    if _EMOTION_RE.search(query) and not ({"pvg_flight_search", "pvg_flight_detail"} & a_tools):
        return "pvg-emotional-support"
    if intent:
        return SKILL_FROM_INTENT.get(intent, "")
    if not a_tools and reply:
        # 无工具纯文本回应（问候/闲聊/拒答/兜底）：对应 B 的 smalltalk skill
        return "pvg-smalltalk"
    return ""


def _infer_b_action(intent: str, reply: str) -> int | None:
    """由推断的 intent 映射 B 的 robot_action.code（依据见 ACTION_FROM_INTENT）。

    A 无 402（真实查询无结果）概念：A 的航班工具空结果会由模型组织语言
    答复，无法可靠识别，因此永不合成 402（402 仅是期望侧的允许值）。
    """

    if intent in ACTION_FROM_INTENT:
        return ACTION_FROM_INTENT[intent]
    if reply:
        return 201  # present_screen：B 中无特定动作时的通用屏幕呈现
    return None


def _plan_from_tool_calls(tool_calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """把 A 的工具调用参数合成为 B semantic.plan 的同构结构，供 _slot_score 使用。

    依据 A 侧工具签名（scripts/pvg_mcp_proxy.py）：
      pvg_flight_search(flightDate, direction, departureCity, arrivalCity, airlineName, ...)
      pvg_flight_detail(flightNo, flightDate, departureAirport, arrivalAirport)
    同轮两次 flight_search 视为往返两段（legs[1].departure_date 在
    _flatten_plan_slots 中同时计入 return_date，与 B 的往返 plan 同构）。
    A 工具无排序偏好参数，preferences 恒为空（sort_preference 槽位必失分，
    属如实记录，见 MAPPING.md）。
    """

    legs: list[dict[str, Any]] = []
    flight_number = ""
    airline = ""
    for name, args in tool_calls:
        leg: dict[str, Any] = {}
        if name == "pvg_flight_search":
            if args.get("departureCity"):
                leg["origin"] = {"value": str(args["departureCity"])}
            if args.get("arrivalCity"):
                leg["destination"] = {"value": str(args["arrivalCity"])}
            if args.get("flightDate"):
                leg["departure_date"] = {"value": str(args["flightDate"])}
            if args.get("airlineName") and not airline:
                airline = str(args["airlineName"])
        elif name == "pvg_flight_detail":
            if args.get("flightNo") and not flight_number:
                flight_number = str(args["flightNo"])
            if args.get("departureAirport"):
                leg["origin"] = {"value": str(args["departureAirport"])}
            if args.get("arrivalAirport"):
                leg["destination"] = {"value": str(args["arrivalAirport"])}
            if args.get("flightDate"):
                leg["departure_date"] = {"value": str(args["flightDate"])}
        if leg:
            legs.append(leg)
    plan: dict[str, Any] = {}
    if flight_number:
        plan["flight_number"] = {"value": flight_number}
    if airline:
        plan["airline"] = {"value": airline}
    if legs:
        plan["legs"] = legs[:2]  # 往返最多两段，与 B 的 plan 结构对齐
    return plan


def _build_actual(turn: ATurnResult, query: str) -> tuple[int, str, dict[str, Any]]:
    """把 A 的单轮结果合成为 B extract_actual 的同构 actual 字典。

    返回 (status, raw, actual)：status 200 表示拿到有效回复（信封字段
    code/has_robot 相应合成），否则 0（判定为传输级 GAP）。
    """

    a_tools = {name for name, _ in turn.tool_calls}
    if turn.error:
        return 0, "", {
            "code": None,
            "error_message": turn.error[:240],
            "has_robot": False,
            "action_code": None,
            "action_name": "",
            "skill": "",
            "intent": "",
            "scene": "",
            "semantic": {},
            "used_tools": sorted(TOOL_MAP_A_TO_B.get(n, n) for n in a_tools),
            "tool_chain": [],
            "domain_arbitration": {},
            "speech_text": "",
        }
    intent = _infer_b_intent(query, turn.reply, a_tools)
    skill = _infer_b_skill(query, intent, a_tools, turn.reply)
    action_code = _infer_b_action(intent, turn.reply)
    plan = _plan_from_tool_calls(turn.tool_calls)
    return 200, turn.reply, {
        "code": 0,
        "error_message": "",
        "has_robot": True,
        "action_code": action_code,
        "action_name": "",
        "skill": skill,
        "intent": intent,
        "scene": "",
        # 不置 candidate：request_kind 检查按 B 逻辑自然跳过；
        # candidate 非 False 时槽位检查生效（与 B 对非候选轮的处理一致）
        "semantic": {"plan": plan},
        "used_tools": sorted(TOOL_MAP_A_TO_B.get(n, n) for n in a_tools),
        "tool_chain": [],
        "domain_arbitration": {},
        "speech_text": turn.reply,
    }


async def _run_case(
    client: httpx.AsyncClient,
    case: dict[str, Any],
    agent_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    """回放单个用例（独立 session，多轮共用）并逐轮按 B 口径判定。"""

    started = time.monotonic()
    deadline = started + timeout_s
    turn_rows: list[dict[str, Any]] = []
    case_verdict = "PASS"
    slot_correct = slot_total = 0

    try:
        resp = await client.post(
            "/sessions",
            json={"agent_id": agent_id, "title": f"cross-a-{case['id']}"},
        )
        resp.raise_for_status()
        session_id = resp.json()["id"]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        # session 创建失败：全部轮次记传输级 GAP
        for turn_index, (text, expected) in enumerate(
            zip(case["turns"], case["expectations"]), start=1
        ):
            turn_rows.append(
                {
                    "turn": turn_index,
                    "input": text,
                    "expected": expected,
                    "actual": None,
                    "status": 0,
                    "latency_s": None,
                    "verdict": "GAP",
                    "reasons": [f"HTTP 0: {type(e).__name__}: {e}"],
                }
            )
        return {
            "id": case["id"],
            "fixture": case["fixture"],
            "category": case.get("category", ""),
            "priority": case.get("priority", ""),
            "verdict": "GAP",
            "turns": turn_rows,
            "slot_correct": 0,
            "slot_total": 0,
        }

    for turn_index, (text, expected) in enumerate(
        zip(case["turns"], case["expectations"]), start=1
    ):
        turn = await _run_turn(client, session_id, text, deadline)
        status, raw, actual = _build_actual(turn, text)
        verdict, reasons, correct, total = judge_turn(expected, status, raw, actual)
        slot_correct += correct
        slot_total += total
        if verdict == "GAP" or (verdict == "PARTIAL" and case_verdict == "PASS"):
            case_verdict = verdict
        turn_rows.append(
            {
                "turn": turn_index,
                "input": text,
                "expected": expected,
                "actual": {
                    "intent": actual.get("intent"),
                    "skill": actual.get("skill"),
                    "action_code": actual.get("action_code"),
                    "used_tools": actual.get("used_tools"),
                    "plan": (actual.get("semantic") or {}).get("plan") or {},
                },
                "status": status,
                "latency_s": round(turn.latency_s, 3) if turn.latency_s is not None else None,
                "verdict": verdict,
                "reasons": reasons,
                "speech_preview": (actual.get("speech_text") or "")[:240],
            }
        )
        if time.monotonic() >= deadline:
            # 超时预算耗尽：剩余轮次不再执行，记 GAP
            for rest_index in range(turn_index + 1, len(case["turns"]) + 1):
                turn_rows.append(
                    {
                        "turn": rest_index,
                        "input": case["turns"][rest_index - 1],
                        "expected": case["expectations"][rest_index - 1],
                        "actual": None,
                        "status": 0,
                        "latency_s": None,
                        "verdict": "GAP",
                        "reasons": ["HTTP 0: case timeout budget exhausted"],
                    }
                )
            case_verdict = "GAP"
            break

    return {
        "id": case["id"],
        "fixture": case["fixture"],
        "category": case.get("category", ""),
        "priority": case.get("priority", ""),
        "verdict": case_verdict,
        "turns": turn_rows,
        "slot_correct": slot_correct,
        "slot_total": slot_total,
    }


def build_report(
    rows: list[dict[str, Any]],
    meta: dict[str, Any],
    all_latencies: list[float],
) -> dict[str, Any]:
    """汇总为交叉评测报告：总 PASS/PARTIAL/GAP + 按 fixture 与 category 聚合。"""

    def aggregate(key: str) -> dict[str, dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            bucket = buckets.setdefault(
                row[key] or "(unknown)",
                {"total": 0, "PASS": 0, "PARTIAL": 0, "GAP": 0},
            )
            bucket["total"] += 1
            bucket[row["verdict"]] += 1
        for bucket in buckets.values():
            bucket["pass_rate"] = round(bucket["PASS"] / bucket["total"], 4)
        return buckets

    verdicts = Counter(row["verdict"] for row in rows)
    slot_correct = sum(row["slot_correct"] for row in rows)
    slot_total = sum(row["slot_total"] for row in rows)
    total = len(rows)
    return {
        "meta": meta,
        "summary": {
            "total": total,
            "PASS": verdicts.get("PASS", 0),
            "PARTIAL": verdicts.get("PARTIAL", 0),
            "GAP": verdicts.get("GAP", 0),
            "pass_rate": round(verdicts.get("PASS", 0) / total, 4) if total else 0.0,
            "by_fixture": aggregate("fixture"),
            "by_category": aggregate("category"),
            "slot_accuracy": round(slot_correct / slot_total, 4) if slot_total else None,
            "slot_correct": slot_correct,
            "slot_total": slot_total,
            "latency_s": {
                "p50": run_eval._percentile(all_latencies, 0.50),
                "p95": run_eval._percentile(all_latencies, 0.95),
            },
        },
        "results": rows,
    }


def print_summary(report: dict[str, Any]) -> None:
    """控制台打印按 fixture / category 的 PASS/PARTIAL/GAP 摘要与非 PASS 列表。"""

    summary = report["summary"]
    print("\n========== 交叉评测摘要（B 语料 → A 系统） ==========")
    for label, buckets in (("fixture", summary["by_fixture"]), ("category", summary["by_category"])):
        print(f"\n--- 按 {label} 聚合 ---")
        print(f"{label:<36}{'总数':>5}{'PASS':>6}{'PART':>6}{'GAP':>5}{'通过率':>9}")
        for name, bucket in sorted(buckets.items()):
            rate = f"{bucket['pass_rate'] * 100:.1f}%"
            print(
                f"{name:<36}{bucket['total']:>5}{bucket['PASS']:>6}"
                f"{bucket['PARTIAL']:>6}{bucket['GAP']:>5}{rate:>9}"
            )
    overall = f"{summary['pass_rate'] * 100:.1f}%"
    print(
        f"\nTOTAL {summary['total']} 例：PASS={summary['PASS']} "
        f"PARTIAL={summary['PARTIAL']} GAP={summary['GAP']}（通过率 {overall}）"
    )
    if summary["slot_accuracy"] is not None:
        print(f"槽位准确率：{summary['slot_accuracy']}（{summary['slot_correct']}/{summary['slot_total']}）")
    latency = summary["latency_s"]
    if latency["p50"] is not None:
        print(f"延迟（wall-clock）：p50={latency['p50']:.2f}s  p95={latency['p95']:.2f}s")

    non_pass = [row for row in report["results"] if row["verdict"] != "PASS"]
    if non_pass:
        print(f"\n非 PASS 用例（{len(non_pass)} 例，最多展示 30 条）：")
        for row in non_pass[:30]:
            reasons = "; ".join(
                reason for turn in row["turns"] for reason in turn["reasons"]
            )
            print(f"  {row['id']} {row['verdict']} [{row['category']}] {reasons[:200]}")


async def run(args: argparse.Namespace) -> int:
    """主流程：加载 fixtures → 过滤 → 并发回放 → 写报告。"""

    fixtures_dir = Path(args.fixtures_dir)
    for name in DEFAULT_FIXTURES:
        if not (fixtures_dir / name).exists():
            print(f"fixture 不存在: {fixtures_dir / name}", file=sys.stderr)
            return 1
    cases = load_cases(fixtures_dir)
    if args.category:
        cases = [c for c in cases if c.get("category") == args.category]
    if args.priority:
        cases = [c for c in cases if c.get("priority") == args.priority]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("过滤后用例为空，请检查 --fixtures-dir/--category/--priority/--limit 参数", file=sys.stderr)
        return 1

    semaphore = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(
        max_connections=max(4, args.concurrency * 2),
        max_keepalive_connections=max(4, args.concurrency * 2),
    )
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), timeout=30.0, limits=limits
    ) as client:

        async def guarded(case: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                row = await _run_case(client, case, args.agent_id, args.timeout)
                print(f"{row['id']} {row['verdict']}", flush=True)
                return row

        print(
            f"开始交叉评测：{len(cases)} 例（B 语料 → A 系统），"
            f"并发 {args.concurrency}，单例超时 {args.timeout}s"
        )
        rows = list(await asyncio.gather(*(guarded(c) for c in cases)))

    all_latencies = [
        turn["latency_s"]
        for row in rows
        for turn in row["turns"]
        if turn.get("latency_s") is not None
    ]
    meta = {
        "direction": "B-corpus -> A-system (Parlant)",
        "base_url": args.base_url,
        "agent_id": args.agent_id,
        "fixtures_dir": str(fixtures_dir),
        "fixtures": list(DEFAULT_FIXTURES),
        "category_filter": args.category,
        "priority_filter": args.priority,
        "limit": args.limit,
        "concurrency": args.concurrency,
        "timeout_s": args.timeout,
        "scoring": "忠实移植 pvg-agent-demo scripts/eval_complete_robot_corpus.py judge_turn；"
        "A 侧 intent/skill/action/plan 由映射表合成（近似点见 evaluation/cross/MAPPING.md）",
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    report = build_report(rows, meta, all_latencies)

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = report_dir / f"cross-a-{stamp}.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    report_path.write_text(payload, encoding="utf-8")
    # 固定文件名副本，便于脚本取最新报告
    (report_dir / "cross-a-latest.json").write_text(payload, encoding="utf-8")

    print_summary(report)
    print(f"\n报告已写入：{report_path}")
    return 0


def main() -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="交叉评测：B 的 621 例语料打 A（Parlant），按 B 的 PASS/PARTIAL/GAP 口径判定")
    parser.add_argument("--fixtures-dir", default=DEFAULT_FIXTURES_DIR, help="B 侧 fixtures 目录（4 个 JSON 文件所在目录）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8800", help="Parlant server 地址")
    parser.add_argument("--agent-id", default="xyVHBNLLPg", help="被测 agent id")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 例")
    parser.add_argument("--category", default=None, help="只跑指定 category")
    parser.add_argument("--priority", default=None, help="只跑指定 priority（P0/P1/P2）")
    parser.add_argument("--concurrency", type=int, default=4, help="并发用例数，默认 4")
    parser.add_argument("--timeout", type=float, default=240.0, help="单用例总超时秒数（含多轮），默认 240")
    parser.add_argument("--report-dir", default="evaluation/reports", help="报告输出目录")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
