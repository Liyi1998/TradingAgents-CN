"""Generation-time public/local partition, independent of Jev and original decisions.

Markers provide lossless structural boundaries, NOT proof of financial truth or
semantic completeness. Every non-whitespace source character must be accounted
for. Missing/malformed partitions never invalidate the original report.
"""
import hashlib
import re

VERSION = "strategy-partition-v1"
MARKERS = tuple(f"<!-- strategy:{name} -->" for name in ("public", "account-unheld", "account-held", "end"))
GENERATION_INSTRUCTION = """
【公开研究与账户约束分区；不改变研究职责、周期、原裁决或完整性要求】
在生成交易计划/最终裁决时，content 全文按以下四个独占行标记组织；不新增模型调用，
保留原本的完整分析、证据日期、价格口径、替代路径、前置条件、退出/否决条件和授权限制。
<!-- strategy:public -->
这里写完整公开研究（可保留原章节与表格）：分别说明假设未持仓/已持仓的公开条件，
未来研究与当前批准分开；缺证据如实写未知。不要把公开否决或退出条件移到本地块。
不写实际账户状态、真实股数、成本、余额、凭据、内网；公开事实保留来源名称、日期及引用，
引用链接仍保留在原报告，外发时不传链接；不能以链接代替必要证据正文。
不要写“参见账户规则”之类隐含依赖，把依赖具体列入下面相应块。
<!-- strategy:account-unheld -->
仅写未持仓分支依赖的实际账户执行约束；未知明确写未知，不猜数值。
<!-- strategy:account-held -->
仅写已持仓分支依赖的实际成本、既有止损/集中度等账户执行约束；未知保持未知。
<!-- strategy:end -->
没有某分支账户依赖时对应块留空（不写“无/已满足”），不为了通过检查删掉必要条件。
共享账户约束在两个本地块均列出。全文不能有标记外内容、嵌套/重复标记或代码围栏。
这些标记只帮助系统另存公开策略和本地账户约束；不是新的交易授权，也不证明条件成立。
"""


def partition_strategy(content: str) -> dict:
    source = content if isinstance(content, str) else ""
    result = {"version": VERSION, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
              "status": "missing", "public_strategy": "", "account_constraints": {}}
    if not any(marker in source for marker in MARKERS):
        return result
    result["status"] = "invalid"
    if any(source.count(marker) != 1 for marker in MARKERS):
        return result
    # Exact ordered blocks; no discarded headers/footnotes or private suffix.
    pattern = r"\s*" + re.escape(MARKERS[0]) + r"\r?\n(.*?)\r?\n" + re.escape(MARKERS[1])
    pattern += r"\r?\n(.*?)\r?\n" + re.escape(MARKERS[2]) + r"\r?\n(.*?)\r?\n" + re.escape(MARKERS[3]) + r"\s*"
    match = re.fullmatch(pattern, source, re.S)
    if not match or not match[1].strip() or "<!-- strategy:" in "".join(match.groups()):
        return result
    if re.search(r"(?m)^\s*(```|~~~)", source):
        return result
    result.update(status="partitioned", public_strategy=match[1],
                  account_constraints={"unheld": match[2], "held": match[3]})
    return result


def local_blocked_branches(partition: dict) -> list[str]:
    """Local text NEVER goes outbound. An unresolved rule is not a passed rule."""
    local = partition.get("account_constraints") or {}
    blocked = {name for name in ("unheld", "held") if str(local.get(name) or "").strip()}
    # A misfiled shared/new-entry condition cannot silently authorize entry.
    if re.search(r"新增|建仓|买入|共享|共同|两类|两种|两分支", str(local.get("held") or "")):
        blocked.add("unheld")
    if re.search(r"持仓|旧仓|存量|减仓|卖出|退出|共享|共同|两类|两种|两分支", str(local.get("unheld") or "")):
        blocked.add("held")
    return sorted(blocked)
