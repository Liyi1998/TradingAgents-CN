"""Reuse report judgments without inventing phases, dates or trading authority."""
from datetime import datetime, timezone
import math
import re

from app.models.wyckoff import WyckoffAssessment
from app.engine.wyckoff_method import METHOD_CONTRACT, METHOD_VERSION
from pydantic import ValidationError

PHASES = {"accumulation": "吸筹", "testing": "试盘", "markup": "拉升", "distribution": "派发", "markdown": "下跌", "uncertain": "待判断"}
WYCKOFF_POLICY = METHOD_VERSION + "-submission-v6"
CONTRACT = METHOD_CONTRACT + """同时提交wyckoff结构化判断，method_version=wyckoff-method-v2。
wyckoff.phase使用accumulation/markup/distribution/markdown/uncertain；不再把testing当作循环阶段。
trend另填up/down/range/uncertain，events逐项填日期、candidate/observed及逐字原文，不把事件名塞进阶段名。
trading_range只填有日线支撑的先前区间；start/end/support/resistance及日期价格须见source_quote。
吸筹的supported状态需区间及后续测试证据，仅SC/AR用candidate；这只是支持假说，不是反转证明。
previous_wyckoff存在时change_reason须逐字引用正文的维持/修订原因；另以alternative_scenario记录替代解释。
跨阶段待验证时status=transition，
candidate_phase为目标阶段，不能提前当作转换完成。status=supported仅表示本次量价证据支持，非交易授权或真实主力行为证明。
在正文中说明当前阶段、反证、后续确认/失效条件；source_quote逐字引用该段正文并包含所选中文阶段名。
source_quote使用“趋势背景：…；当前阶段：…”分别列示；observed事件也只是事件解释获得量价支持，不是主力意图证明。
evidence_ids只引用本轮提供的同源不复权日线，必须包含基准日的证据。没有足够量价时使用uncertain并说明限制。
structure_start仅在本轮行情覆盖、且正文明确写出YYYY-MM-DD起始日时填写，否则null；不得拿分析日冒充结构起始日。
已观察到的价格/量能与阶段推断要区分；日线不能确认分时承接、真实资金主体、未结束周线或除权引发的假突破。
confirmation_conditions和invalidation_conditions保留必要前提，未知条件不能删除。缺项不阻塞其他研究。
提交前自检：events.kind只可SC/AR/ST/Spring/Test/SOS/LPS/BC/UT/UTAD/SOW/LPSY中的单个值；这些斜线是枚举分隔，禁止PSY/BC、Spring/SC等合并事件。PSY、JAC等其他观察写正文，不将其改名伪装成已核验事件。
candidate_phase仅status=transition时填写，status=candidate/supported/uncertain时必须null。不得为通过校验删除必要反证或把observed随意替换为candidate。
用一个完整独立段落写“趋势背景：上涨/下跌/横盘/不明……；当前阶段：吸筹/拉升/派发/下跌/待判断……；状态：候选/本轮证据支持/转换待确认/证据不足……”。source_quote复制该完整原段，不能自行拼接、缩写或改写正文。
trend须对应“趋势背景”中的当前描述：up对应上涨/上行，down对应下跌/下行，range对应横盘/震荡整理；此前趋势、未来条件及当前阶段名称不能替代当前趋势依据。混合背景请明确此前与当前，无法区分时用uncertain。
各条件、事件、区间、替代解释和修订原因均逐字复制正文。structure_start不在阶段原段时，用structure_start_quote引用正文中“结构起始日：YYYY-MM-DD”的整句；不写无日线依据的日期。
局部提交校验失败时系统最多允许一次纠正；有依据的技术正文保留。资料不足用uncertain并说明，不能为获得标签编造行情或确认。
Spring/UTAD 的先前区间必须在事件日之前结束，不能拿包含事件当日或后续突破日的整段区间证明该事件。逐项先核对区间结束日 < 事件日及实际越界、收回价格；未证明时正文明确为待核验假说，不得仍写“观察到/已确认”。其他有依据的阶段判断可以保留，但不得依赖未经支持的事件作为确认理由。
引用事件时复制含事件代码和日期的完整原行；修订目标时逐条说明旧目标是否保留、新目标的原始行情日期与OHLC字段、触发条件、已知时间。新增远期阻力不等于当日已突破，不得混用日线高点、收盘价和周线推演价格。
"""


def assessment_error(exc):
    """Retain actionable field paths, never rejected model inputs or credentials."""
    if isinstance(exc, ValidationError):
        errors = exc.errors(include_input=False, include_url=False)
        return "字段校验失败：" + "；".join(
            ".".join(map(str, e["loc"])) + " (" + e["type"] + ")" for e in errors[:8]
        ) + (f"；另有{len(errors)-8}项" if len(errors) > 8 else "")
    return str(exc).splitlines()[0][:240]


def checked_assessment(value, content, snapshot, *, require_method=False):
    result = WyckoffAssessment.model_validate(value).model_dump(mode="json")
    from app.engine.wyckoff_quotes import bind_assessment_quotes
    result = bind_assessment_quotes(result, content)
    if require_method and result["method_version"] != METHOD_VERSION:
        raise ValueError("阶段判断缺少新版方法字段，不能按已核验阶段展示")
    quote = result["source_quote"]
    if quote not in content:
        raise ValueError("阶段依据必须逐字出现在本次技术正文中")
    for phase in (result["phase"], result["candidate_phase"]):
        if phase and phase != "uncertain" and PHASES[phase] not in quote:
            raise ValueError("阶段与引用正文不一致")
    if result["status"] == "transition":
        if not result["candidate_phase"] or result["candidate_phase"] in {result["phase"], "uncertain"}:
            raise ValueError("转换判断需要不同的候选阶段")
    elif result["candidate_phase"] is not None:
        raise ValueError("仅待验证的转换可填写候选阶段")
    if result["phase"] == "uncertain" and result["status"] != "uncertain":
        raise ValueError("未知阶段不能标记已获得支持")
    bars = {b["date"]: b for b in sorted(snapshot.get("bars", []), key=lambda b: b["date"]) if b["date"] <= snapshot["as_of"] and not b.get("status")}
    ids = {b.get("evidence_id"): b for b in bars.values() if b.get("evidence_id")}
    if not set(result["evidence_ids"]).issubset(ids):
        raise ValueError("阶段引用了未核验或不同口径的日线证据")
    if result["phase"] != "uncertain":
        current = snapshot.get("price") or {}
        if current.get("date") != snapshot["as_of"] or current.get("evidence_id") not in ids or current.get("evidence_id") not in result["evidence_ids"]:
            raise ValueError("当前阶段缺少本轮收盘日线证据")
        if len(bars) < 20 or any(type(b.get("volume")) not in (int, float) or not math.isfinite(b["volume"]) or b["volume"] < 0 for b in list(bars.values())[-20:]):
            raise ValueError("至少需要20根量价完整的日线；不足时保留未知")
    start = result["structure_start"]
    start_quote = result.get("structure_start_quote") or ""
    if start_quote and (start_quote not in content or not re.search(r"结构(?:起始|开始)(?:日|日期)?", start_quote)):
        raise ValueError("结构起始日的独立依据须逐字引用正文并明确标明结构开始")
    if start and (start not in bars or (start not in quote and start not in start_quote) or start > snapshot["as_of"]):
        raise ValueError("结构起始日须在本次日线窗口内并逐字见于依据")
    for key in ("confirmation_conditions", "invalidation_conditions"):
        if any(not text.strip() or len(text) > 600 or text not in content for text in result[key]):
            raise ValueError("确认与失效条件须完整引用技术正文，每项不超过600字")
    if result["status"] in {"candidate", "transition"} and not result["confirmation_conditions"]:
        raise ValueError("候选或转换判断须保留确认条件")
    if result["method_version"] == METHOD_VERSION:
        check_method_fields(result, content, bars, snapshot.get("previous_wyckoff"), strict_trend=require_method)
    return result


def check_method_fields(result, content, bars, previous=None, *, strict_trend=True):
    """Necessary structural checks, not a statistical proof of market intent."""
    if result["phase"] == "testing" or result["candidate_phase"] == "testing":
        raise ValueError("试盘属于测试行为，不是独立循环阶段")
    if result["phase"] != "uncertain" and not result["invalidation_conditions"]:
        raise ValueError("阶段假说须保留失效条件")
    if result["phase"] != "uncertain":
        plain = re.sub(r"[*`_【】]", "", result["source_quote"])
        judgment = re.search(r"(?:当前阶段|阶段假说|阶段判断|阶段定性)\s*[：:为]\s*([^；。\n]+)", plain)
        if not judgment or not judgment[1].startswith(PHASES[result["phase"]]):
            raise ValueError("必须分列当前阶段，不能把旧阶段、趋势或被否定的阶段当作本次结论")
    if result["status"] in {"candidate", "transition"} and not re.search(r"候选|待(?:确认|验证)|尚待|可能|潜在|假说", result["source_quote"]):
        raise ValueError("候选或转换状态必须在阶段原文中明确，不能仅在隐藏字段中降级")
    if result["status"] == "supported" and not re.search(r"证据支持|量价支持|结构支持", result["source_quote"]):
        raise ValueError("获得支持的状态须与阶段正文一致，不能把候选标成已支持")
    from app.engine.wyckoff_trend import check_trend_quote
    try:
        check_trend_quote(result["trend"], result["source_quote"])
    except ValueError:
        # Immutable historical judgments already accepted under the old rule
        # remain readable. Every new full/daily submission requests strict mode.
        legacy_word = {"up": "上涨", "down": "下跌", "range": "横盘"}.get(result["trend"])
        if strict_trend or not legacy_word or legacy_word not in result["source_quote"]:
            raise
    for key in ("change_reason", "alternative_scenario"):
        text = result[key]
        if text is not None and (not text.strip() or text not in content):
            raise ValueError("变更理由与替代解释须逐字引用正文")
    if previous and not result["change_reason"]:
        raise ValueError("须交代相对上次判断的维持或修订原因")
    trading_range = result["trading_range"]
    if trading_range:
        start, end = trading_range["start"], trading_range["end"]
        quote = trading_range["source_quote"]
        if start not in bars or end not in bars or start >= end or quote not in content or start not in quote or end not in quote:
            raise ValueError("交易区间日期和原文依据不完整")
        support, resistance = trading_range["support"], trading_range["resistance"]
        window = [b for d, b in bars.items() if start <= d <= end]
        numbers = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", quote)]
        if support >= resistance or not all(any(abs(x-y) < 1e-6 for y in numbers) for x in (support, resistance)):
            raise ValueError("区间价格须与正文一致且下沿低于上沿")
        if not all(type(b.get(k)) in (int, float) and math.isfinite(b[k]) for b in window for k in ("low", "high")):
            raise ValueError("区间缺少高低价依据")
        if not min(b["low"] for b in window) <= support < resistance <= max(b["high"] for b in window):
            raise ValueError("区间边界超出所引日线价格范围")
    observed = []
    for event in result["events"]:
        quote, day = event["source_quote"], event["date"]
        if quote not in content or day not in quote or day not in bars or event["kind"].lower() not in quote.lower():
            raise ValueError("事件须绑定本轮日线日期及含事件名的原文")
        if bars[day].get("evidence_id") not in result["evidence_ids"]:
            raise ValueError("事件日期未引用对应日线证据")
        if event["status"] != "observed":
            continue
        observed.append(event)
        if event["kind"] in {"Spring", "UTAD"}:
            if not trading_range or trading_range["end"] >= day:
                raise ValueError(f"{event['kind']}({day})需先前交易区间：区间结束日{trading_range['end'] if trading_range else '缺失'}必须早于事件日；不能引用包含该事件或后来突破的区间")
            bar = bars[day]
            later = [b for d, b in bars.items() if d >= day]
            if event["kind"] == "Spring" and not (bar["low"] < trading_range["support"] and any(b["close"] >= trading_range["support"] for b in later)):
                raise ValueError("所引行情未验证Spring下探后收回区间")
            if event["kind"] == "UTAD" and not (bar["high"] > trading_range["resistance"] and any(b["close"] <= trading_range["resistance"] for b in later)):
                raise ValueError("所引行情未验证UTAD上冲后回落区间")
    sc = [e["date"] for e in observed if e["kind"] == "SC"]
    ar = [e["date"] for e in observed if e["kind"] == "AR"]
    st = [e["date"] for e in observed if e["kind"] in {"ST", "Test"}]
    if (sc and ar and min(ar) <= min(sc)) or (ar and st and min(st) <= min(ar)):
        raise ValueError("SC、AR及后续测试的已观察事件顺序不一致")
    if result["phase"] == "accumulation" and result["status"] == "supported":
        if not trading_range or not st or not any(d > trading_range["end"] for d in st):
            raise ValueError("仅SC/AR或单次反弹不足以支持吸筹阶段；保留候选并列出测试条件")
    if result["phase"] == "accumulation" and not result["alternative_scenario"]:
        raise ValueError("吸筹假说须保留下跌中继/再派发等替代解释")


def baseline_assessment(baseline):
    """Conservative verbatim extraction; headings/examples/future branches are not judgments."""
    text = (baseline.get("reports") or {}).get("market_report") or ""
    if not isinstance(text, str):
        return None
    if "威科夫方法校验：阶段结构未通过校验" in text:
        return None
    lines = text.splitlines()
    inside, level = False, 0
    candidates = []
    for i, raw in enumerate(lines):
        heading = re.match(r"^(#{1,6})\s+(.+)", raw.strip())
        if heading:
            if "威科夫" in heading[2] or "wyckoff" in heading[2].lower():
                inside, level = True, len(heading[1])
            elif len(heading[1]) <= level:
                inside = False
        if not inside:
            continue
        plain = re.sub(r"[*`_【】]", "", raw).strip()
        judgment = re.search(r"(?:当前(?:所处)?阶段(?:定性|判断)?|阶段定性|阶段判断|当前(?:标的|股票|股价)?处于)\s*[：:]?", plain)
        if not judgment:
            continue
        # Avoid reading a conditional forecast or rejection as a current phase.
        if re.search(r"^(?:[-\d.\s]*)?(?:若|如果|假如|一旦|假设|例如|示例)", plain):
            continue
        phase_text = re.split(r"[；。]", plain[judgment.end():], maxsplit=1)[0]
        found = list(dict.fromkeys(re.findall(r"吸筹|试盘|拉升|派发|下跌", phase_text)))
        explicit_unknown = bool(re.match(r"\s*(?:待判断|证据不足|不确定|无法判定|无法判断)", phase_text))
        if (not found and not explicit_unknown) or len(raw) > 1800:
            continue
        reverse = {v: k for k, v in PHASES.items()}
        transition = len(found) == 2 and bool(re.search(r"(?:向|转|过渡)", plain))
        uncertain = explicit_unknown or len(found) > 1 and not transition or bool(re.search(r"无法|不能确认|未确认|不属于|并非|不是|尚未进入|排除|不支持", plain))
        candidates.append({"phase": "uncertain" if uncertain else reverse[found[0]],
            "candidate_phase": reverse[found[1]] if transition and not uncertain else None,
            "status": "uncertain" if uncertain else "transition" if transition else "candidate",
            "source_quote": raw.strip(), "source_line": i+1, "structure_start": None,
            "evidence_ids": [], "confirmation_conditions": [], "invalidation_conditions": [],
            "limitations": ["原报告判断，仅作起点参考；尚未由每日研究复核。结构起始日未自动推定。"]})
    if len(candidates) != 1:
        return None  # Ambiguous reports remain available through their report link.
    return candidates[0]


def stamp(value):
    if isinstance(value, datetime):
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat()
    return str(value) if value else None


def annotation(assessment, *, as_of, known_at, source_kind, analysis_id, version_id=None):
    return {**assessment, "as_of": as_of, "known_at": stamp(known_at), "source_kind": source_kind,
        "source_analysis_id": analysis_id, "version_id": version_id}


def unvalidated_judgment(content, as_of, kind, known_at):
    """Show the actual newest statement separately, never a phase annotation."""
    from app.engine.wyckoff_repair import section_bounds
    bounds = section_bounds(content)
    if not bounds:
        return None
    section = content[bounds[0]:bounds[1]]
    lines = [line.strip() for line in section.splitlines()
             if re.search(r"当前阶段\s*[：:为]|阶段定性\s*[：:为]", re.sub(r"[*`_]", "", line))]
    if len(lines) != 1 or len(lines[0]) > 1800:
        return None
    return {"as_of": as_of, "source_kind": kind, "known_at": stamp(known_at),
            "source_quote": lines[0], "status": "unvalidated"}


def wyckoff_board(baseline, versions, selected):
    """Caller supplies owner/baseline/cutoff-filtered versions; never write history."""
    origin = baseline_assessment(baseline)
    history = [annotation(origin, as_of=baseline["as_of"], known_at=baseline.get("created_at"),
        source_kind="report", analysis_id=baseline["analysis_id"])] if origin else []
    for row in sorted(versions, key=lambda r: (r["created_at"], str(r["_id"]))):
        technical = (row.get("modules") or {}).get("technical") or {}
        value = technical.get("wyckoff")
        # Inherited modules retain their original timestamp, not this version's date.
        if not value or technical.get("inherited") or technical.get("update_error") or technical.get("wyckoff_error") or technical.get("as_of") != row.get("as_of"):
            continue
        try:
            value = checked_assessment(value, technical["content"], row["snapshot"])
        except (ValueError, KeyError, TypeError):
            continue
        history.append(annotation(value, as_of=technical["as_of"], known_at=row["created_at"],
            source_kind="daily", analysis_id=baseline["analysis_id"], version_id=str(row["_id"])))
    current = history[-1] if history else None
    technical = ((selected or {}).get("modules") or {}).get("technical") or {}
    if not selected:
        notice = "原报告起点判断；每日研究生成后随技术模块复核。" if origin else "原报告未提供可明确提取的当前阶段，请查看技术报告；后续每日研究会单独更新阶段。"
        state = "baseline"
    elif technical.get("update_error") or technical.get("wyckoff_error"):
        state = "failed"
        notice = ("本版本威科夫更新未完成，以下保留上次判断及原日期。" if current else
            "本版本威科夫更新未完成；当前研究起点下暂无通过校验的阶段判断。") + "其他研究继续显示。"
    elif not technical.get("wyckoff"):
        state, notice = "missing", "本版本未提供结构化阶段，未将旧判断当作今天的结论。后续技术更新会提交阶段。"
    elif technical.get("inherited"):
        state, notice = ("inherited", "量价材料未变化，沿用上次阶段及原识别时间。") if current else (
            "missing", "本版本沿用了技术正文，但尚无可校验的阶段记录；未把沿用正文当作新阶段。")
    elif current and current["version_id"] == str(selected.get("_id", selected.get("id"))):
        state, notice = "current", "本版本技术研究已更新阶段；阶段推断不等于买卖授权。"
    else:
        state, notice = "missing", "本版本阶段依据未通过校验，未绘制新的阶段。"
    if selected and not selected.get("review"):
        notice += " 本版本综合复核尚未完成。"
    error = (technical.get("wyckoff_repair") or {}).get("error") or technical.get("wyckoff_error") or technical.get("update_error")
    if selected and not technical:
        error = next((e for e in selected.get("errors", []) if e.startswith("技术更新：")), error)
        if error:
            state, notice = "failed", "本次技术更新未完成；历史有效判断保留原日期，新报告正文另列为待核验。"
    pending = None
    if technical.get("wyckoff_error"):
        pending = unvalidated_judgment(technical.get("content", ""), technical.get("as_of"), "daily", selected.get("created_at"))
    text = (baseline.get("reports") or {}).get("market_report") or ""
    if not pending and "威科夫方法校验：阶段结构未通过校验" in text:
        pending = unvalidated_judgment(text, baseline["as_of"], "report", baseline.get("created_at"))
        if not error:
            error = text.split("\n\n", 1)[0]
    return {"current": current, "history": history, "status": state, "notice": notice,
        "pending_judgment": pending,
        "error": error, "error_details_missing": bool(error and re.search(r"\d+ validation errors? for", error)),
        "source_analysis_id": baseline["analysis_id"], "source_date": baseline["as_of"]}
