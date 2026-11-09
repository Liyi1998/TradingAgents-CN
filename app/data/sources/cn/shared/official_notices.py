"""Free CNINFO disclosures, exact-symbol matching and bounded PDF excerpts.

Full filings are discovered separately from recent announcements. Keyword excerpts
are evidence locations, not a claim that an entire financial note was extracted.
"""

import asyncio
import copy
import io
import logging
import re
import time
from datetime import datetime, timedelta
from functools import lru_cache
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from app.data.storage.cache.memory_cache import TTLCache

logger = logging.getLogger(__name__)
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.cninfo.com.cn/"}
_CACHE = TTLCache(default_ttl=1800)
_LOCKS = {}
_NOTE_GROUPS = {
    "restricted_cash": ("受限货币资金", "受限资金", "所有权或使用权受到限制", "使用受到限制", "受限"),
    "debt_maturity": ("一年内到期", "到期日", "长期借款", "短期借款", "应付债券"),
    "guarantees": ("对外担保", "担保情况", "担保余额", "担保"),
    "impairment": ("商誉减值", "信用减值", "资产减值", "减值准备"),
    "receivables": ("应收账款", "账龄"),
    "related_parties": ("关联交易", "关联方交易"),
}


@lru_cache(maxsize=1)
def _stock_ids():
    response = requests.get("https://www.cninfo.com.cn/new/data/szse_stock.json", headers=_HEADERS, timeout=(3, 5))
    response.raise_for_status()
    return {str(item["code"]): item["orgId"] for item in response.json()["stockList"]}


def select_note_excerpts(page_texts: list[str], total_pages: int, scan_complete: bool) -> tuple[str, dict]:
    """Select multiple topics on the same page and bounded continuation pages."""
    pools = {group: [] for group in _NOTE_GROUPS}
    blank_pages = []
    for index, text in enumerate(page_texts):
        if len(text.strip()) < 30:
            blank_pages.append(index + 1)
        if "目录" in re.sub(r"\s+", "", text[:200]):
            continue
        for group, words in _NOTE_GROUPS.items():
            offsets = sorted({match.start() for word in words for match in re.finditer(re.escape(word), text)})
            for offset in offsets[:12]:
                begin, end = max(0, offset - 200), min(len(text), offset + 850)
                excerpt = text[begin:end]
                score = min(200, len(re.findall(r"\d", excerpt))) + min(index, 30)
                pools[group].append((score, index, begin, end))
            pools[group].sort(reverse=True)
            del pools[group][8:]
    selected, spans, refs, chars = [], {}, [], 0
    covered = set()

    def add(index, begin, end, group, continuation=False):
        nonlocal chars
        if chars >= 16000:
            return False
        # Do not drop a different note just because it occurs on the same PDF page.
        for a, b in spans.get(index, []):
            if a <= begin and end <= b:
                return True
        text = page_texts[index][begin:end][:16000 - chars]
        if not text.strip():
            return False
        selected.append(f"[PDF页{index + 1};主题:{group};{'续页' if continuation else '关键词片段'}] {text}")
        spans.setdefault(index, []).append((begin, begin + len(text)))
        unit = re.search(r"(?:单位\s*[:：]?\s*)?(人民币)?\s*(亿元|万元|千元|元)", text)
        refs.append({"page": index + 1, "topic": group, "start": begin, "end": begin + len(text),
                     "continuation": continuation, "unit_hint": unit.group(0).strip() if unit else None})
        chars += len(text)
        return True

    # Round-robin first: a verbose receivables note must not starve guarantees.
    for rank in range(2):
        for group, pool in pools.items():
            if len(pool) > rank:
                _, index, begin, end = pool[rank]
                if add(index, begin, end, group):
                    covered.add(group)
                if rank == 0 and index + 1 < len(page_texts) and end >= len(page_texts[index]) - 100:
                    add(index + 1, 0, min(650, len(page_texts[index + 1])), group, True)
    if not selected:
        for index, text in enumerate(page_texts[:2]):
            add(index, 0, min(800, len(text)), "document_identity")
    metadata = {"status": "partial", "total_pages": total_pages, "scanned_pages": len(page_texts),
                "scan_complete": scan_complete, "page_limit": 200, "excerpt_chars": chars,
                "blank_or_scanned_pages": blank_pages, "ocr_performed": False, "evidence": refs,
                "topics_with_excerpts": sorted(covered), "missing_topics": sorted(set(_NOTE_GROUPS) - covered),
                "limitations": ["keyword_excerpts_not_complete_notes", "tables_not_structurally_verified"]}
    if not scan_complete:
        metadata["limitations"].append("page_or_time_budget_exhausted")
    if blank_pages:
        metadata["limitations"].append("image_or_empty_pages_require_ocr")
    return "\n".join(selected).strip(), metadata


def extract_official_pdf_with_metadata(url: str) -> tuple[str, str, dict]:
    """No arbitrary URL access; excerpts are not a full-document representation."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "static.cninfo.com.cn" or parsed.port not in (None, 443) or not parsed.path.lower().endswith(".pdf"):
        return "", "invalid_official_url", {"status": "unavailable"}
    try:
        from pypdf import PdfReader

        deadline = time.monotonic() + 10
        payload = bytearray()
        with requests.get(url, headers=_HEADERS, timeout=(3, 3), stream=True, allow_redirects=False) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("unexpected redirect/status")
            for chunk in response.iter_content(65536):
                payload.extend(chunk)
                if len(payload) > 12 * 1024 * 1024 or time.monotonic() > deadline:
                    raise ValueError("PDF exceeds bounded download budget")
        if not payload.startswith(b"%PDF"):
            raise ValueError("not a PDF")
        reader = PdfReader(io.BytesIO(payload), strict=False)
        page_texts = []
        for index, page in enumerate(reader.pages[:200]):
            if time.monotonic() > deadline + 5:
                break
            page_texts.append(page.extract_text() or "")
        content, metadata = select_note_excerpts(page_texts, len(reader.pages), len(page_texts) == len(reader.pages))
        status = (f"pdf_targeted_notes_excerpt_scanned_{len(page_texts)}_of_{len(reader.pages)}_pages_max_16000_chars"
                  f";topic_hits={','.join(metadata['topics_with_excerpts'])};not_full_note_coverage")
        metadata["source_url"] = url
        return content, status if content else "pdf_text_unavailable", metadata
    except Exception as exc:
        logger.debug("巨潮PDF正文获取失败: %s", exc)
        return "", "pdf_fetch_failed", {"status": "unavailable", "error_type": type(exc).__name__}


def extract_official_pdf(url: str) -> tuple[str, str]:
    content, status, _ = extract_official_pdf_with_metadata(url)
    return content, status


def classify_filing(title: str) -> dict | None:
    """Only a full annual/semiannual report is a financial-note source."""
    clean = re.sub(r"\s+", "", title)
    match = re.search(r"(20\d{2})年?(半年度|中期|年度)报告", clean)
    if not match or any(word in clean for word in ("摘要", "英文", "英语", "英文版", "取消")):
        return None
    is_full = not any(word in clean for word in ("公告", "说明", "提示", "问询", "意见", "审议"))
    revised = any(word in clean for word in ("更正", "修订", "更新"))
    if not is_full and not revised:
        return None
    return {"report_year": int(match.group(1)), "report_kind": "annual" if match.group(2) == "年度" else "semiannual",
            "full_report": is_full, "revised": revised,
            "report_period_end": match.group(1) + ("-12-31" if match.group(2) == "年度" else "-06-30")}


def select_filings(items: list[dict], as_of: str) -> list[dict]:
    """Use the latest disclosed full version per latest annual/semiannual period."""
    groups = {}
    for item in items:
        info = classify_filing(item.get("title", ""))
        if not info or not item.get("publish_time") or item["publish_time"][:10] > as_of:
            continue
        groups.setdefault((info["report_kind"], info["report_year"]), []).append(dict(item, filing_metadata=info))
    chosen = []
    for kind in ("annual", "semiannual"):
        available = sorted((key for key in groups if key[0] == kind), reverse=True)
        for key in available:
            full = [item for item in groups[key] if item["filing_metadata"]["full_report"]]
            if not full:
                continue
            full.sort(key=lambda item: (item["publish_time"], item["filing_metadata"]["revised"]), reverse=True)
            selected = full[0]
            corrections = [item for item in groups[key] if not item["filing_metadata"]["full_report"] and item["publish_time"] >= selected["publish_time"]]
            selected["filing_metadata"].update(as_of=as_of, disclosure_date=selected["publish_time"][:10],
                revision_status="correction_notice_requires_reconciliation" if corrections else "latest_full_version_in_bounded_search",
                correction_notices=[{"title": item["title"], "url": item["url"], "disclosure_date": item["publish_time"][:10]} for item in corrections[:3]])
            chosen.append(selected)
            break
    return sorted(chosen, key=lambda item: item["filing_metadata"]["report_period_end"], reverse=True)


async def fetch_official_notices(symbol: str, limit: int = 10, end_date: str = None) -> list[dict]:
    if not re.fullmatch(r"\d{6}", symbol):
        return []

    if limit <= 0:
        return []
    end = datetime.strptime(end_date[:10], "%Y-%m-%d") if end_date else datetime.now(ZoneInfo("Asia/Shanghai"))
    cutoff = end.strftime("%Y-%m-%d")
    cache_key = f"filings:{symbol}:{cutoff}:{min(limit, 20)}"

    def fetch():
        from concurrent.futures import ThreadPoolExecutor

        org_id = _stock_ids().get(symbol)
        if not org_id:
            return []
        deadline = time.monotonic() + 14

        def query(category):
            items, pages, exhausted = [], 0, False
            for page in range(1, (3 if category else 1) + 1):
                if time.monotonic() > deadline:
                    break
                response = requests.post("https://www.cninfo.com.cn/new/hisAnnouncement/query", headers=_HEADERS,
                    data={"pageNum": page, "pageSize": 50 if category else min(limit, 20), "column": "szse", "tabName": "fulltext", "plate": "",
                          "stock": f"{symbol},{org_id}", "searchkey": "", "category": category, "isHLtitle": "false",
                          "seDate": f"{(end - timedelta(days=420 if category else 90)).strftime('%Y-%m-%d')}~{cutoff}"}, timeout=(3, 4))
                response.raise_for_status()
                payload = response.json()
                pages += 1
                for notice in payload.get("announcements") or []:
                    if str(notice.get("secCode")) != symbol:
                        continue
                    stamp = notice.get("announcementTime")
                    published = datetime.fromtimestamp(float(stamp) / 1000, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S") if stamp else ""
                    if not published or published[:10] > cutoff:
                        continue
                    items.append({"symbol": symbol, "title": re.sub(r"<[^>]+>", "", str(notice.get("announcementTitle") or "")),
                        "publish_time": published, "url": "https://static.cninfo.com.cn/" + str(notice.get("adjunctUrl") or "").lstrip("/"),
                        "source": "巨潮资讯（上市公司官方披露）", "data_source": "akshare", "category": "official_announcement",
                        "source_kind": "official", "content": "", "content_status": "title_only"})
                if not payload.get("hasMore"):
                    exhausted = True
                    break
            return items, {"category": category or "recent", "pages": pages, "search_exhausted": exhausted}

        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = [pool.submit(query, category) for category in ("category_ndbg_szsh;", "category_bndbg_szsh;", "")]
            rows, channels = [], []
            for job in jobs:
                try:
                    items, metadata = job.result()
                    rows.extend(items)
                    channels.append(metadata)
                except Exception as exc:
                    channels.append({"status": "unavailable", "error_type": type(exc).__name__})
        selected = select_filings(rows, cutoff)
        seen = {item["url"] for item in selected}
        selected.extend(item for item in sorted(rows, key=lambda item: item["publish_time"], reverse=True)
                        if item["url"] not in seen and not seen.add(item["url"]))
        for item in selected[:min(limit, 20)]:
            item["discovery_metadata"] = {"as_of": cutoff, "filing_lookback_days": 420, "channels": channels,
                "max_query_requests": 7, "max_pdf_requests": 3, "latest_filings_prioritized": True,
                "missing_filing_kinds": sorted({"annual", "semiannual"} - {i["filing_metadata"]["report_kind"] for i in selected if "filing_metadata" in i})}
        return selected[:min(limit, 20)]

    try:
        lock = _LOCKS.setdefault((id(asyncio.get_running_loop()), cache_key), asyncio.Lock())
        async with lock:
            cached = _CACHE.get(cache_key)
            if cached is not None:
                return copy.deepcopy(cached)
            items = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=18)
            # Financial filings occupy the first slots; recent unrelated PDFs cannot displace them.
            try:
                excerpts = await asyncio.wait_for(asyncio.gather(*(asyncio.to_thread(extract_official_pdf_with_metadata, item["url"]) for item in items[:3])), timeout=16)
            except asyncio.TimeoutError:
                excerpts = [("", "pdf_fetch_timeout", {"status": "unavailable"}) for _ in items[:3]]
            for item, (content, status, metadata) in zip(items, excerpts):
                item.update(content=content, content_status=status, pdf_metadata=metadata)
            if items:
                _CACHE.set(cache_key, copy.deepcopy(items))
            return items
    except Exception as exc:
        logger.warning("巨潮公告通道暂不可用 %s: %s", symbol, exc)
        return []
