"""Seed PVG PoC data into a running Parlant server via the REST API.

将 PRD P0 数据资产写入运行中的 Parlant 服务（幂等，可重复执行）：

  1. Glossary 术语（FR-106）
  2. 高合规话术模板 canned responses（FR-201/202/203）
     - 固定口径模板（投诉安抚、转人工、失物、拒答、值机建议、延误安抚、退改签）
     - 动态知识槽位模板（{{knowledge_answer}}/{{knowledge_source}}、{{flight_info}}，
       字段数据由 pvg_knowledge_search / 航班工具经 canned_response_fields 提供）
  3. Journey 多步流程（FR-501/502/503）：失物招领、投诉与转人工、退改签引导
  4. 红线规则（FR-103）：不得承诺赔偿/费用具体金额
  5. composition mode 治理：agent 默认 canned_fluid；航班/行李/政策三条
     高合规 guideline 设为 composited_canned（FR-203）

用法：
  uv run python scripts/seed_pvg_poc_data.py            # 实际写入
  uv run python scripts/seed_pvg_poc_data.py --dry-run  # 只打印计划
环境变量：PARLANT_BASE_URL（默认 http://127.0.0.1:8800）
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx

BASE_URL = os.environ.get("PARLANT_BASE_URL", "http://127.0.0.1:8800").rstrip("/")
AGENT_ID = "xyVHBNLLPg"  # 浦东机场智能客服
AGENT_TAG = f"agent:{AGENT_ID}"

# agent 默认组合模式：fluid + 有匹配模板时用模板（FR-201 的落地形态）
AGENT_COMPOSITION_MODE = "canned_fluid"

# 高合规 guideline → composited_canned（FR-203：模板骨架 + 工具/RAG 动态数据）
# 如需 FR-202 的 STRICT 实验，把对应值改为 "strict_canned"
GUIDELINE_COMPOSITION_OVERRIDES = {
    "7kc3ZzOBlT": "composited_canned",  # 航班查询（flight_info 槽位）
    "lA1yP0TReY": "composited_canned",  # 行李规定（knowledge 槽位）
    "nLStZZtqM5": "composited_canned",  # 政策/安检/服务信息（knowledge 槽位）
}

# ---------------------------------------------------------------------------
# FR-106 Glossary 术语
# ---------------------------------------------------------------------------

TERMS: list[dict[str, Any]] = [
    {"name": "值机", "description": "旅客办理登机手续的过程，包括换取登机牌与托运行李；浦东机场支持柜台值机、自助值机与小程序在线值机。",
     "synonyms": ["办理登机", "check-in", "办票"]},
    {"name": "托运行李", "description": "在值机时交付航空公司装入货舱运输的行李，有重量与尺寸限制，具体以承运航司规定为准。",
     "synonyms": ["checked baggage", "托运行李额"]},
    {"name": "随身携带行李", "description": "旅客带入客舱自行照管的行李，通常限 1 件、重量与尺寸受限，锂电池/充电宝只能随身携带不得托运。",
     "synonyms": ["手提行李", "随身行李", "carry-on"]},
    {"name": "超售", "description": "航空公司售出的座位数超过航班实际可售座位数的做法；发生拒载时旅客可按非自愿情况获得相应安排与补偿。",
     "synonyms": ["oversold", "航班超售"]},
    {"name": "非自愿降舱", "description": "因航空公司原因（机型变更、超售等）旅客被迫从高等级舱位调整到低等级舱位，可按规定获得差价退还与补偿。",
     "synonyms": ["降舱"]},
    {"name": "经停", "description": "航班在起点与终点之间降落一个中间机场，旅客通常不下飞机或短暂停留后继续同一航班号的行程。",
     "synonyms": ["经停航班", "stopover flight"]},
    {"name": "中转", "description": "旅客在途中机场换乘另一航班继续行程；涉及行李是否直挂、是否需要重新值机与安检。",
     "synonyms": ["转机", "transit", "connecting flight"]},
    {"name": "行李直挂", "description": "联程行程中行李在始发站一次托运、直达最终目的地，中转站无需提取重新托运。",
     "synonyms": ["行李联运", "through check"]},
    {"name": "卫星厅", "description": "浦东机场 S1/S2 卫星厅，与 T1/T2 主楼通过捷运系统连接，部分登机口位于卫星厅。",
     "synonyms": ["S1", "S2", "卫星航站楼"]},
    {"name": "捷运系统", "description": "连接浦东机场 T1/T2 主楼与 S1/S2 卫星厅的旅客自动输送系统（APM），免费乘坐。",
     "synonyms": ["APM", "机场捷运"]},
    {"name": "磁悬浮", "description": "连接浦东机场与龙阳路站的磁浮列车，全程约 8 分钟，是往返市区的快速方式之一。",
     "synonyms": ["磁浮", "maglev"]},
    {"name": "液态物品限制", "description": "乘坐国内航班随身携带液态物品单件容器不超过 100ml、总量不超过 1L，需装于透明可封口塑料袋；超出须托运。",
     "synonyms": ["100ml 限制", "液体限制"]},
    {"name": "充电宝额定能量", "description": "充电宝/锂电池以额定能量（Wh）计量：不超过 100Wh 可随身携带；100–160Wh 须经航司批准；超过 160Wh 禁止携带；一律不得托运。",
     "synonyms": ["Wh", "充电宝限制"]},
    {"name": "违禁品", "description": "民航禁止随身携带或托运的物品，如枪支弹药、易燃易爆品、管制刀具等；以民航局与机场安检公布目录为准。",
     "synonyms": ["禁带物品", "危险品"]},
    {"name": "无人陪伴儿童", "description": "无成人陪同单独乘机的 5–12 周岁儿童，需提前向航空公司申请专项服务，机场与航司提供交接陪护。",
     "synonyms": ["UM", "无陪儿童"]},
    {"name": "航变", "description": "航班发生延误、取消、时刻变更等计划外变动；航变导致的退改签一般属非自愿退改，免收手续费（以航司政策为准）。",
     "synonyms": ["航班变动", "航班取消", "非自愿退改"]},
]

# ---------------------------------------------------------------------------
# FR-201/202/203 话术模板
# ---------------------------------------------------------------------------

CANNED_RESPONSES: list[dict[str, Any]] = [
    # —— 固定口径模板（无动态字段）——
    {
        "value": "非常抱歉给您带来了不好的体验，您的反馈我们非常重视。您可以拨打浦东机场服务质量监督热线 021-96990 反映问题，也可以在小程序内提交工单，会有专人跟进处理。您愿意先跟我说说具体情况吗？我会如实记录。",
        "fields": [],
        "signals": ["我要投诉", "你们服务太差了", "我要反映问题"],
        "metadata": {"scenario": "complaint", "compliance": "high"},
    },
    {
        "value": "好的，为您转接人工服务。人工客服工作时间为每日 06:00–24:00；非工作时间您可以拨打机场服务热线 021-96990，或在小程序留言，我们会尽快回复您。",
        "fields": [],
        "signals": ["转人工", "我要找人工客服", "你们有没有真人客服"],
        "metadata": {"scenario": "handoff", "compliance": "high"},
    },
    {
        "value": "抱歉，目前失物招领系统中暂时没有查到相符的拾获记录。线上暂时无法办理报失登记，建议您前往航站楼问讯柜台登记，或通过小程序「失物招领」栏目办理；捡到相符物品后机场会与您联系，也建议您过几天再来查询。",
        "fields": [],
        "signals": ["没找到我丢的东西", "查不到我的失物", "怎么挂失"],
        "metadata": {"scenario": "lost_and_found", "compliance": "high"},
    },
    {
        "value": "您好，我是浦东机场智能客服，只能解答机场出行相关的问题，比如航班动态、值机、行李、安检、市内交通和机场设施等。请问您有这方面的需要吗？",
        "fields": [],
        "signals": ["帮我写作业", "你会做什么", "讲个笑话"],
        "metadata": {"scenario": "out_of_scope", "compliance": "medium"},
    },
    {
        "value": "建议您：乘坐国内航班至少提前 2 小时、国际及港澳台航班至少提前 3 小时到达机场。T1、T2 与卫星厅 S1/S2 之间需乘捷运或步行，请预留充足时间；节假日客流高峰建议再提前一些。",
        "fields": [],
        "signals": ["提前多久到机场", "几点到机场合适", "值机要提前多久"],
        "metadata": {"scenario": "checkin_time", "compliance": "medium"},
    },
    {
        "value": "非常抱歉，航班延误给您带来不便了。航班延误后的退改签与赔偿政策以承运航空公司的规定为准，您可以联系航空公司客服或通过购票平台办理；如果需要，我也可以先帮您查询该航班的最新动态。",
        "fields": [],
        "signals": ["航班延误有赔偿吗", "延误了怎么办", "飞机晚点怎么补偿"],
        "metadata": {"scenario": "delay", "compliance": "high", "red_line": "no_amount"},
    },
    {
        "value": "机票的退票和改签需要通过承运航空公司或原购票平台办理，机场客服无法直接操作。建议您：1）拨打航空公司官方客服；2）在购票平台的订单页申请；3）如果是航班延误、取消等航变导致的，可按非自愿退改签政策办理。具体费用与规则以航空公司和购票平台公示为准。",
        "fields": [],
        "signals": ["我要退票", "怎么改签", "退票手续费多少"],
        "metadata": {"scenario": "refund_change", "compliance": "high", "red_line": "no_amount"},
    },
    # —— 动态知识槽位模板（FR-203，字段由工具经 canned_response_fields 提供）——
    {
        "value": "根据机场官方信息，{{knowledge_answer}}\n（依据：{{knowledge_source}}；如有临时调整，请以机场现场公告为准。）",
        "fields": [
            {"name": "knowledge_answer",
             "description": "必须严格依据 pvg_knowledge_search 工具返回的知识内容作答，保留原文中的数字、时间、限制条件，不得编造或推测",
             "examples": ["额定能量不超过 100Wh 的充电宝可随身携带，禁止托运"]},
            {"name": "knowledge_source",
             "description": "pvg_knowledge_search 返回结果中对应知识条目的 title 原文",
             "examples": ["锂电池、充电宝携带规定"]},
        ],
        "signals": ["充电宝能带上飞机吗", "安检有什么规定", "液体能带多少", "有什么违禁品"],
        "metadata": {"scenario": "policy", "compliance": "high"},
    },
    {
        "value": "关于行李规定，机场官方说明如下：{{knowledge_answer}}\n（依据：{{knowledge_source}}）\n温馨提示：具体免费行李额以承运航空公司规定为准，超重或特殊行李建议提前咨询航司。",
        "fields": [
            {"name": "knowledge_answer",
             "description": "必须严格依据 pvg_knowledge_search 工具返回的知识内容作答，保留原文中的数字、尺寸、重量限制，不得编造或推测",
             "examples": ["随身携带行李限 1 件，重量不超过 5 公斤"]},
            {"name": "knowledge_source",
             "description": "pvg_knowledge_search 返回结果中对应知识条目的 title 原文",
             "examples": ["随身携带行李规定"]},
        ],
        "signals": ["行李能带多少", "托运行李规定", "行李箱尺寸要求"],
        "metadata": {"scenario": "baggage", "compliance": "high"},
    },
    {
        "value": "为您查到：{{flight_info}}\n航班动态可能随时调整，建议出发前再次通过小程序「航班动态」确认。",
        "fields": [
            {"name": "flight_info",
             "description": "来自航班查询工具返回的航班号、日期、航站楼、登机口、计划与预计起降时间、航班状态；逐项如实引用，不得编造或推测",
             "examples": ["MU5101，7 月 20 日，T1 航站楼，计划 10:30 起飞，目前状态正常"]},
        ],
        "signals": ["帮我查一下航班", "MU5101 几点起飞", "哪个航站楼登机"],
        "metadata": {"scenario": "flight", "compliance": "high"},
    },
]

# ---------------------------------------------------------------------------
# FR-501/502/503 Journey 多步流程（REST 建壳 + trigger，步骤用 journey tag 的 guideline 表达）
# ---------------------------------------------------------------------------

JOURNEYS: list[dict[str, Any]] = [
    {
        "title": "失物招领协助",
        "description": "旅客报失物品时的多步引导：收集物品信息 → 检索拾获记录 → 告知领取或登记方式",
        "triggers": ["用户报失物品", "用户询问丢失物品怎么办", "用户寻找在机场遗失的物品"],
        "steps": [
            {"condition": "失物招领协助中，用户尚未提供遗失物品的名称或特征",
             "action": "询问遗失物品的名称、特征、大致遗失时间与地点（哪个航站楼或区域），一次不要问太多问题"},
            {"condition": "失物招领协助中，用户已提供遗失物品信息",
             "action": "若用户直接询问能否在线登记报失，先如实告知线上暂不支持报失登记，应前往航站楼问讯柜台或通过小程序「失物招领」栏目办理；再调用 pvg_lost_and_found_search 帮用户检索拾获记录，找到则告知物品名称、拾获时间与地点及领取方式，未找到则建议过几天再来查询",
             "tools": ["pvg_lost_and_found_search"]},
        ],
    },
    {
        "title": "投诉与转人工",
        "description": "旅客投诉或要求人工服务时的安抚、记录与转接流程",
        "triggers": ["用户要投诉", "用户要求转人工客服", "用户情绪激动且问题未得到解决"],
        "steps": [
            {"condition": "投诉与转人工流程中，用户刚开始表达不满",
             "action": "先安抚并致歉，认真倾听不打断，复述确认用户的诉求要点；同一会话内不重复致歉"},
            {"condition": "投诉与转人工流程中，用户诉求已明确或坚持要求人工",
             "action": "告知会通过小程序工单跟进，提供浦东机场服务质量监督热线 021-96990；用户坚持人工时，说明人工客服工作时间为每日 06:00–24:00 并为其转接；转接后不再主动回复"},
        ],
    },
    {
        "title": "退改签引导",
        "description": "旅客退票/改签需求的分流引导：区分自愿与非自愿，指引正确办理渠道，不承诺金额",
        "triggers": ["用户要退票", "用户要改签", "用户询问退改签费用或规则"],
        "steps": [
            {"condition": "退改签引导中，用户提出退票或改签需求",
             "action": "说明退改签需通过承运航空公司或原购票平台办理，机场客服无法直接操作；询问是自愿退改还是航班变动（延误/取消）导致"},
            {"condition": "退改签引导中，用户因航班延误或取消而办理退改",
             "action": "告知航班变动导致的退改签一般属非自愿退改、可免手续费，具体以航空公司政策为准，引导联系航司客服或在购票平台办理；不得承诺或估算任何具体金额"},
        ],
    },
]

# ---------------------------------------------------------------------------
# FR-103 红线规则
# ---------------------------------------------------------------------------

EXTRA_GUIDELINES: list[dict[str, Any]] = [
    {
        "condition": "用户询问航班延误赔偿、退改签费用或其他涉及具体金额的问题",
        "action": "不得承诺、估算或编造任何具体赔偿金额与费用数字；说明赔偿与费用以承运航空公司及购票平台公示政策为准，引导用户联系航空公司客服或查看购票平台规则",
        "criticality": "high",
        "metadata": {"red_line": "no_amount", "compliance": "high"},
    },
    {
        "condition": "用户询问某个城市或机场的三字码、机场名称、机场所在城市等机场信息",
        "action": "必须先调用 pvg_airport_search 查询机场字典，严格依据工具返回结果回答，不得凭记忆作答；查不到时如实说明并建议以航空公司或机场官方信息为准",
        "criticality": "medium",
        # FR-404 条件式工具供给：工具仅在 guideline 命中时对模型可见
        "tools": ["pvg_airport_search"],
    },
    {
        "condition": "用户的航班延误或取消，用户着急、不满或询问怎么办",
        "action": "先安抚并表达理解，再提供帮助：主动提出查询航班最新动态，说明航变导致的退改签一般属非自愿退改（以航司政策为准）；同一会话内不重复致歉",
        "criticality": "medium",
    },
]


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, timeout=60.0)


def _get_all(c: httpx.Client, path: str) -> list[dict[str, Any]]:
    r = c.get(path)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("items", [])


def seed_terms(c: httpx.Client, dry: bool) -> tuple[int, int]:
    existing = {t["name"] for t in _get_all(c, "/terms")}
    created = skipped = 0
    for term in TERMS:
        if term["name"] in existing:
            skipped += 1
            continue
        if not dry:
            r = c.post("/terms", json={**term, "tags": [AGENT_TAG]})
            r.raise_for_status()
        created += 1
    return created, skipped


def seed_canned_responses(c: httpx.Client, dry: bool, recreate: bool = False) -> tuple[int, int]:
    if recreate:
        # Parlant 的向量库为内存瞬态实现，server 重启后 canned response 的
        # embedding 索引丢失，而幂等跳过不会重建——导致 composited_canned
        # 选择失败（回复退化为兜底话术）。删除重建以强制刷新向量索引。
        doomed = [cr for cr in _get_all(c, "/canned_responses") if AGENT_TAG in (cr.get("tags") or [])]
        if not dry:
            for cr in doomed:
                r = c.delete(f"/canned_responses/{cr['id']}")
                if r.status_code >= 400:
                    print(f"  [warn] 删除 canned response {cr['id']} 失败：{r.status_code}",
                          file=sys.stderr)
        print(f"话术模板：--recreate-canned 删除存量 {len(doomed)} 条")
    existing = {cr["value"] for cr in _get_all(c, "/canned_responses")}
    created = skipped = 0
    for cr in CANNED_RESPONSES:
        if cr["value"] in existing:
            skipped += 1
            continue
        if not dry:
            r = c.post("/canned_responses", json={**cr, "tags": [AGENT_TAG]})
            r.raise_for_status()
        created += 1
    return created, skipped


def seed_journeys(c: httpx.Client, dry: bool) -> tuple[int, int]:
    existing_journeys = {j["title"]: j["id"] for j in _get_all(c, "/journeys")}
    existing_conditions = {g["condition"]: g["id"] for g in _get_all(c, "/guidelines")}
    created = skipped = 0
    for j in JOURNEYS:
        jid = existing_journeys.get(j["title"])
        if jid is None:
            if not dry:
                r = c.post("/journeys", json={
                    "title": j["title"],
                    "description": j["description"],
                    "triggers": j["triggers"],
                    "tags": [AGENT_TAG],
                })
                r.raise_for_status()
                jid = r.json()["id"]
            created += 1
        else:
            skipped += 1
        for step in j["steps"]:
            tools = step.get("tools", [])
            step_gid = existing_conditions.get(step["condition"])
            if step_gid is None:
                if not dry:
                    payload = {k: v for k, v in step.items() if k != "tools"}
                    r = c.post("/guidelines", json={
                        **payload,
                        "criticality": "medium",
                        "tags": [AGENT_TAG, f"journey:{jid}"],
                    })
                    r.raise_for_status()
                    step_gid = r.json()["id"]
            if tools and step_gid is not None and not dry:
                _associate_tools(c, step_gid, tools)
    return created, skipped


def _associate_tools(c: httpx.Client, guideline_id: str, tool_names: list[str]) -> None:
    r = c.patch(f"/guidelines/{guideline_id}", json={
        "tool_associations": {
            "add": [{"service_name": "pvg", "tool_name": t} for t in tool_names],
        },
    })
    if r.status_code >= 400:
        print(f"  [warn] guideline {guideline_id} 工具关联失败：{r.status_code} {r.text[:200]}",
              file=sys.stderr)


def seed_extra_guidelines(c: httpx.Client, dry: bool) -> tuple[int, int]:
    existing = {g["condition"]: g["id"] for g in _get_all(c, "/guidelines")}
    created = skipped = 0
    for g in EXTRA_GUIDELINES:
        tools = g.get("tools", [])
        gid = existing.get(g["condition"])
        if gid is None:
            if not dry:
                payload = {k: v for k, v in g.items() if k != "tools"}
                r = c.post("/guidelines", json={**payload, "tags": [AGENT_TAG]})
                r.raise_for_status()
                gid = r.json()["id"]
            created += 1
        else:
            skipped += 1
        if tools and gid is not None and not dry:
            _associate_tools(c, gid, tools)
    return created, skipped


def apply_composition_modes(c: httpx.Client, dry: bool) -> None:
    if not dry:
        r = c.patch(f"/agents/{AGENT_ID}", json={"composition_mode": AGENT_COMPOSITION_MODE})
        r.raise_for_status()
        for gid, mode in GUIDELINE_COMPOSITION_OVERRIDES.items():
            r = c.patch(f"/guidelines/{gid}", json={"composition_mode": mode})
            if r.status_code >= 400:
                print(f"  [warn] guideline {gid} composition_mode 设置失败：{r.status_code} {r.text[:200]}",
                      file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的变更，不写入")
    parser.add_argument("--recreate-canned", action="store_true",
                        help="删除并重建本 agent 的话术模板（server 重启后刷新向量索引，"
                             "修复 composited_canned 选择失败）")
    args = parser.parse_args()
    dry = args.dry_run

    print(f"目标服务：{BASE_URL}，agent：{AGENT_ID}{'（dry-run）' if dry else ''}")
    with _client() as c:
        terms = seed_terms(c, dry)
        print(f"Glossary 术语：新增 {terms[0]}，跳过 {terms[1]}")
        canned = seed_canned_responses(c, dry, recreate=args.recreate_canned)
        print(f"话术模板：新增 {canned[0]}，跳过 {canned[1]}")
        journeys = seed_journeys(c, dry)
        print(f"Journey 流程：新增 {journeys[0]}，跳过 {journeys[1]}（步骤 guideline 随建）")
        guidelines = seed_extra_guidelines(c, dry)
        print(f"红线规则：新增 {guidelines[0]}，跳过 {guidelines[1]}")
        apply_composition_modes(c, dry)
        print(f"composition mode：agent={AGENT_COMPOSITION_MODE}，"
              f"高合规 guideline {len(GUIDELINE_COMPOSITION_OVERRIDES)} 条=composited_canned")
    print("完成。")


if __name__ == "__main__":
    main()
