"""Read-only annotations from explicit report price fields, never new decisions.

Legacy reports need no model call or database rewrite. These references retain
their original wording and date; they are not a substitute for a daily review.
"""
import math
import re

from app.engine.strategy_partition import partition_strategy


def plain(text):
    return re.sub(r"[*`]", "", text).strip().strip('_')


def explicit_exit_clause(text):
    """Narrow public execution-clause grammar; never mine general prose."""
    value = re.sub(r"^[-+•]\s*", "", text)
    if value == text or not value.startswith("若"):
        return None
    number = r"(?:0|[1-9]\d*)(?:\.\d+)?"
    price = rf"(?P<lower>{number})\s*元(?:\s*[-–—~～至到]\s*(?P<upper>{number})\s*元)?"
    trigger = re.match(rf"^若(?P<prefix>[^。；;]{{0,100}}?(?:触及|延伸至|击穿|跌破)[^。；;]{{0,60}}?){price}(?P<rest>.*)$", value)
    if trigger:
        if re.search(r"未|不|没有|取消|驳回|例如|假设|[\d._+-]\s*$", trigger["prefix"]):
            return None
        rest = trigger["rest"]
        action = re.match(r"^[，,](?P<prefix>[^。；;]{0,20}?)(?P<action>减仓|清空|清仓|卖出)", rest)
        if not action or re.search(r"不|未|禁止|取消|驳回|例如|假设|无需|毋须|暂缓", action["prefix"]):
            return None
        label = "反抽减仓参考" if action["action"] == "减仓" else "破位退出参考" if re.search(r"击穿|跌破", trigger["prefix"]) else "反抽清仓参考"
        return label, "exit", float(trigger["lower"]), float(trigger["upper"] or trigger["lower"]), value
    observation = re.match(rf"^若[^。；;]{{0,40}}[，,]以\s*{price}[（(][^）)]*支撑[^）)]*[）)]作为动态观察线[；;。]?$", value)
    if observation:
        return "支撑观察线", "support", float(observation["lower"]), float(observation["upper"] or observation["lower"]), value
    return None


def structured_public_levels(public, source_lines, baseline, known_at):
    """Read explicit execution columns/conditional parameters with branch context.

    Only marked public final-plan sections qualify. Full conditional sections are
    retained so a future hypothetical stop cannot become a current held stop.
    """
    lines = public.splitlines()
    headings, result = {}, []
    number = r"(?:0|[1-9]\d*)(?:\.\d+)?"
    price = re.compile(rf"^(?:(?:日线)?收盘价|盘中)?(?:有效)?(?:跌破|击穿|触及)?\s*({number})(?:\s*元)?(?:\s*[-–—~～至到]\s*({number}))?\s*元\s*(?=[（(，,；;。]|$)")

    def add(label, value, kind, branch, quote, condition):
        matched = price.match(value)
        if not matched or re.search(r"美元|港元|港币|USD|HKD|前复权|后复权|qfq|hfq|已作废|已取消", condition, re.I):
            return
        low, high = float(matched[1]), float(matched[2] or matched[1])
        positions = [i + 1 for i, line in enumerate(source_lines) if line == quote]
        if len(positions) != 1 or not all(map(math.isfinite, (low, high))) or not 0 < low <= high:
            return
        result.append({"kind": kind, "label": label, "lower": low, "upper": high,
            "branch": branch, "condition": condition, "source_quote": quote, "source_line": positions[0],
            "source_date": baseline["as_of"], "source_analysis_id": baseline["analysis_id"],
            "effective_from": max(baseline["as_of"], known_at[:10]), "valid_until": None,
            "expired": False, "state": "report_reference", "currency": "CNY",
            "price_basis": "raw" if re.search(r"不复权|\braw\b", condition, re.I) else "unspecified"})

    for index, line in enumerate(lines):
        heading = re.match(r"^\s*(#{1,6})\s+(.+)$", line)
        if not heading:
            continue
        depth, title = len(heading[1]), plain(heading[2])
        headings = {d: t for d, t in headings.items() if d < depth}
        headings[depth] = title
        context = " / ".join(headings.values())
        if re.search(r"驳回|否决|废弃|未采纳|原始|旧方案|账户约束", context):
            continue
        end = next((i for i in range(index + 1, len(lines)) if re.match(rf"^\s*#{{1,{depth}}}\s+", lines[i])), len(lines))
        section = lines[index:end]
        if re.search(r"已持仓执行指令|已持仓.*执行手册|价格参数.*执行梯队", title) and re.search(r"最终.*(?:执行|交易)", context):
            header = None
            columns = None
            for row in section[1:]:
                if not row.strip().startswith('|'):
                    header = None
                    continue
                cells = [plain(cell) for cell in row.strip().strip('|').split('|')]
                schema = next((keys for keys in [('触发场景', '执行价格/条件', '执行动作'),
                    ('梯队阶段', '价格参考区间', '仓位调度指令')] if all(key in cells for key in keys)), None)
                if schema:
                    header, columns = cells, schema
                    continue
                if not header or len(cells) != len(header):
                    continue
                values = dict(zip(header, cells))
                label_key, price_key, action_key = columns
                label, action = values[label_key], values[action_key]
                branch = 'unheld' if '未持仓' in label else 'held'
                if not re.search(r"减仓|减持|清仓|清空|止损|出清|卖出|建仓|加仓", action) or re.search(r"不|未|禁止|取消|驳回|暂缓", action):
                    continue
                kind = 'entry' if branch == 'unheld' else 'target' if '目标' in label else 'exit'
                add(('未持仓条件·' if branch == 'unheld' else '已持仓·') + label, values[price_key], kind, branch, row,
                    context + '：' + '；'.join(k + '：' + v for k, v in values.items()))
        elif '未持仓' in title and re.search(r"条件.*研究方案", title) and re.search(r"非实时|非当前", title):
            full_context = '\n'.join(section)
            if len(full_context) > 6000 or not re.search(r"前置条件|必须全部满足", full_context):
                continue  # don't truncate a scenario's prerequisites
            for row in section[1:]:
                item = re.match(r"^[-+•]\s*(观察基准支撑位|条件入场位|保护性止损位|第一反弹目标位)\s*[:：]\s*(.+)$", plain(row))
                if item:
                    kind = 'support' if '支撑' in item[1] else 'entry' if '入场' in item[1] else 'target' if '目标' in item[1] else 'exit'
                    add('未持仓条件·' + item[1], item[2], kind, 'unheld', row, full_context)
    return result


def report_chart_levels(baseline, known_at):
    reports = baseline.get("reports") or {}
    key = "final_trade_decision" if reports.get("final_trade_decision") else "trader_investment_plan"
    source = reports.get(key) or ""
    if not isinstance(source, str):
        return [], "原报告正文格式无法识别，其他行情与研究内容仍保留"
    partition = partition_strategy(source)
    if partition["status"] == "invalid":
        return [], "原报告公开研究分区不完整，未自动提取点位"
    public = partition["public_strategy"] if partition["status"] == "partitioned" else source
    # Account blocks are excluded above. Without a marked partition, only an
    # explicitly named public price section is eligible, never free-form prose.
    source_lines = source.splitlines()
    public_lines = public.splitlines()
    active_depth = None
    price_list_indent = None
    mode = "prices"
    context = ""
    headings = {}
    levels = []
    if partition["status"] == "partitioned":
        levels = [{**p, "source_section": key} for p in structured_public_levels(public, source_lines, baseline, known_at)]
    for line in public_lines:
        heading = re.match(r"^\s*(#{1,6})\s+(.+)$", line)
        if heading:
            if price_list_indent is not None:
                active_depth = None
            price_list_indent = None
            depth, title = len(heading[1]), plain(heading[2])
            headings = {d: t for d, t in headings.items() if d < depth}
            headings[depth] = title
            ancestry = " / ".join(headings.values())
            if active_depth and depth <= active_depth:
                active_depth = None
            if re.search(r"关键(?:价格)?点位|关键价格|核心交易参数", title) and not re.search(r"账户|驳回|否决|废弃|未采纳", ancestry):
                active_depth, context, mode = depth, ancestry, "prices"
            elif partition["status"] == "partitioned" and re.search(r"具体清仓执行节奏", title) and not re.search(r"驳回|否决|废弃|未采纳|账户约束", ancestry):
                active_depth, context, mode = depth, ancestry, "exit_clauses"
            continue
        # Some final decisions use a named nested list rather than a Markdown
        # heading. Only its indented price fields belong to this scope; adjacent
        # debate/account paragraphs and dated ladders must stay outside it.
        title = plain(line)
        if partition["status"] == "partitioned" and re.fullmatch(r"[-+•]\s*(?:核心基准价格区间|关键价格点位|关键点位)\s*[:：]", title) and not re.search(r"账户|驳回|否决|废弃|未采纳", " / ".join(headings.values())):
            price_list_indent = len(line)-len(line.lstrip())
            active_depth, context, mode = max(headings, default=1), " / ".join(headings.values()) + " / " + title, "prices"
            continue
        if price_list_indent is not None and line.strip() and len(line)-len(line.lstrip()) <= price_list_indent:
            price_list_indent, active_depth = None, None
        if not active_depth:
            continue
        text = plain(line)
        clause = explicit_exit_clause(text) if mode == "exit_clauses" else None
        if mode == "exit_clauses":
            if not clause or re.search(r"美元|港元|港币|USD|HKD|前复权|后复权|qfq|hfq", context + text, re.I):
                continue
            label, kind, lower, upper, value = clause
            if not all(map(math.isfinite, (lower, upper))) or not 0 < lower <= upper:
                continue
            positions = [i+1 for i, original in enumerate(source_lines) if original == line]
            if len(positions) != 1:
                continue
            levels.append({"kind": kind, "label": label, "lower": lower, "upper": upper,
                "condition": context + "：" + value, "source_quote": line, "source_section": key,
                "source_line": positions[0], "source_date": baseline["as_of"], "source_analysis_id": baseline["analysis_id"],
                "effective_from": max(baseline["as_of"], known_at[:10]), "valid_until": None,
                "expired": False, "state": "report_reference",
                "price_basis": "raw" if re.search(r"不复权|\braw\b", context + value, re.I) else "unspecified", "currency": "CNY"})
            continue
        match = re.match(r"^[-+•]\s*(.{2,60}?)\s*[:：]\s*(.+)$", text)
        if not match:
            # Two-column label/value tables only. Dated execution tables have
            # separate per-day meanings and must not be flattened into ranges.
            cells = [plain(x) for x in text.strip('|').split('|')]
            if text.startswith('|') and len(cells) == 2:
                label, value = cells
            else:
                continue
        else:
            label, value = match.groups()
        if re.search(r"账户|成本|估值|公允|收益|仓位上限|仓位比例|持仓比例|基准|现价|驳回|否决|废弃|未采纳", label):
            continue
        kind = next((k for pattern, k in (
            (r"支撑", "support"), (r"阻力|压力", "resistance"), (r"目标", "target"),
            (r"止损|保护|失效|减仓|卖出|清仓|退出", "exit"), (r"入场|买入|建仓", "entry")) if re.search(pattern, label)), None)
        if not kind or re.search(r"撤销|取消|废弃|驳回|不采用|已作废", value):
            continue
        number = r"(?:0|[1-9]\d*)(?:\.\d+)?"
        prices = re.match(rf"^([¥￥]?\s*({number}))(?:\s*元(?:/股)?\s*)?(?:\s*[-–—~～至到]\s*({number})(?:\s*元(?:/股)?)?)?\s*(?=[（(，,；;。]|$)", value)
        if not prices or not re.search(r"元|人民币|CNY|¥|￥", context + label + prices[0]):
            continue
        lower, upper = float(prices[2]), float(prices[3] or prices[2])
        if not all(map(math.isfinite, (lower, upper))) or not 0 < lower <= upper or re.search(r"美元|港元|港币|USD|HKD|前复权|后复权|qfq|hfq", context + value, re.I):
            continue
        # Exact line binding, including the parenthetical prerequisites.
        positions = [i+1 for i, original in enumerate(source_lines) if original == line]
        if len(positions) != 1:
            continue
        levels.append({"kind": kind, "label": label, "lower": lower, "upper": upper,
            "condition": value, "source_quote": line, "source_section": key,
            "source_line": positions[0], "source_date": baseline["as_of"],
            "source_analysis_id": baseline["analysis_id"],
            "effective_from": max(baseline["as_of"], known_at[:10]),
            "valid_until": None, "expired": False, "state": "report_reference",
            "price_basis": "raw" if re.search(r"不复权|\braw\b", context + value, re.I) else "unspecified",
            "currency": "CNY"})
    if len(levels) > 16:
        return [], "原报告点位数量过多，需核对原文范围，未截断展示"
    return levels, ("原报告点位仅供对照，尚未由所选每日版本复核，不代替原文完整退出与否决条件；线段延伸不代表有效期延长或交易授权。"
                    if levels else "原报告未找到可逐字绑定的明确价格字段；未从叙述、百分比或日期猜测点位。")
