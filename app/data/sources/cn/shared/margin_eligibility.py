"""Official, date-evidenced eligibility. Missing/incomplete evidence stays unknown."""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from datetime import date
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup


def official_bytes(url: str, *, params=None, max_bytes=8 * 1024 * 1024) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"www.sse.com.cn", "query.sse.com.cn", "www.szse.cn"} or parsed.port not in (None, 443):
        raise ValueError("non-official exchange URL")
    payload, deadline = bytearray(), time.monotonic() + 8
    with requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://{parsed.hostname}/"},
                      timeout=(3, 5), stream=True, allow_redirects=False) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("unexpected exchange redirect/status")
        for chunk in response.iter_content(65536):
            payload.extend(chunk)
            if len(payload) > max_bytes or time.monotonic() > deadline:
                raise ValueError("exchange response exceeds bounded budget")
    return bytes(payload)


def parse_szse_eligibility(payload: list, symbol: str, as_of: str) -> dict:
    """subname is the server result date; query conditions merely echo the request."""
    result = {"verified": False, "reason": "response_date_or_scope_unverified"}
    tabs = [tab for tab in payload if (tab.get("metadata") or {}).get("tabkey") == "tab1"]
    if len(tabs) != 1:
        return result
    meta, rows = tabs[0].get("metadata") or {}, tabs[0].get("data") or []
    if meta.get("catalogid") != "1834_xxpl" or str(meta.get("subname", "")).strip() != as_of:
        return result
    if any(str(row.get("zqdm")) != symbol for row in rows) or int(meta.get("recordcount", -1)) != len(rows):
        return result
    if len(rows) != 1:
        # The exchange returns an undated empty result for absent symbols.
        return dict(result, reason="no_date_evidenced_symbol_record")
    row = rows[0]
    flags = [row.get("rzbz"), row.get("rjbz")]
    if any(flag not in {"Y", "N"} for flag in flags):
        return result
    return {"verified": True, "eligible": "Y" in flags, "evidence_date": as_of,
            "reason": "official_date_evidenced_symbol_record", "evidence_count": 1,
            "financing_eligible": row["rzbz"] == "Y", "shorting_eligible": row["rjbz"] == "Y",
            "financing_available_today": row.get("jrkrz") == "Y", "shorting_available_today": row.get("jrkrj") == "Y"}


def fetch_szse_eligibility(symbol: str, as_of: str) -> dict:
    url = "https://www.szse.cn/api/report/ShowReport/data"
    payload = json.loads(official_bytes(url, params={"SHOWTYPE": "JSON", "CATALOGID": "1834_xxpl",
        "TABKEY": "tab1", "txtDate": as_of, "txtZqdm": symbol, "tab1PAGENO": "1"}))
    return dict(parse_szse_eligibility(payload, symbol, as_of), source="SZSE", source_url=url)


def parse_sse_snapshot(text: str, as_of: str) -> dict:
    stamp = re.search(r'staticDate\s*:\s*["\'](\d{4}-\d{2}-\d{2})', text)
    rows = re.findall(r"\[\s*'(\d+)'\s*,\s*'(\d{6})\s*'\s*,", text)
    sequences = [int(row[0]) for row in rows]
    complete = (bool(rows) and sequences == list(range(1, len(rows) + 1))
                and len({row[1] for row in rows}) == len(rows) and bool(re.search(r"\]\s*\}\s*;\s*$", text)))
    return {"verified": bool(stamp and stamp.group(1) == as_of and complete),
            "evidence_date": stamp.group(1) if stamp else None, "codes": {row[1] for row in rows},
            "evidence_count": len(rows), "reason": "official_dated_current_snapshot"}


def parse_sse_index(html: str, expected_page: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    paging = re.search(r"createPageHTML\('paging',\s*(\d+),\s*(\d+),\s*'s_list',\s*'shtml',\s*(\d+)\)", html)
    if not paging or int(paging.group(2)) != expected_page:
        raise ValueError("SSE adjustment pagination unverified")
    rows = []
    for dd in soup.find_all("dd"):
        published, anchor = re.search(r"20\d{2}-\d{2}-\d{2}", dd.get_text()), dd.find("a", href=True)
        if not published or not anchor:
            raise ValueError("SSE adjustment index has an incomplete entry")
        rows.append({"published": published.group(0), "title": anchor.get_text(), "url": anchor["href"]})
    count = min(15, max(0, int(paging.group(3)) - (expected_page - 1) * 15))
    if len(rows) != count or [row["published"] for row in rows] != sorted((row["published"] for row in rows), reverse=True):
        raise ValueError("SSE adjustment index incomplete or unsorted")
    return rows


def parse_sse_adjustment(html: str, url: str) -> dict:
    """Only explicit code + effective date + unambiguous add/remove events apply."""
    soup = BeautifulSoup(html, "html.parser")
    paragraphs = "\n".join(node.get_text(" ", strip=True) for node in soup.find_all("p"))
    effective = re.search(r"本所[于自]\s*(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日起", paragraphs)
    actions = set(re.findall(r"调([入出])融资融券标的证券", paragraphs))
    codes = set(re.findall(r"(?<!\d)([569]\d{5})(?!\d)", paragraphs))
    if not effective or len(actions) != 1 or not codes:
        return {"verified": False, "source_url": url}
    effective_date = date(*(int(part) for part in effective.groups())).isoformat()
    return {"verified": True, "effective_date": effective_date, "eligible": actions == {"入"},
            "codes": codes, "source_url": url}


def fetch_sse_eligibility(symbol: str, as_of: str) -> dict:
    """Current full list, or bounded quarterly positive membership + complete events.

    A quarterly list excludes registration-based IPOs. Absence from it can NEVER
    establish non-eligibility. Older dates outside the discovered baseline fail closed.
    """
    base = "https://www.sse.com.cn"
    snapshot_url = base + "/services/tradingservice/margin/info/againstmargin/data_961.js"
    snapshot = parse_sse_snapshot(official_bytes(snapshot_url).decode("utf-8"), as_of)
    if snapshot["verified"] and symbol in snapshot["codes"]:
        return {key: value for key, value in dict(snapshot, eligible=True, source="SSE", source_url=snapshot_url).items() if key != "codes"}
    unknown = {"verified": False, "reason": "historical_eligibility_evidence_incomplete", "source": "SSE",
               "source_url": snapshot_url, "snapshot_date": snapshot["evidence_date"]}
    deadline = time.monotonic() + 20
    home = BeautifulSoup(official_bytes(base + "/services/tradingservice/margin/home/").decode("utf-8"), "html.parser")
    anchors = [a for a in home.find_all("a", href=True) if "季度定期调整" in a.get_text() and "/lawandrules/" in a["href"]]
    if not anchors:
        return unknown
    anchors.sort(key=lambda anchor: anchor["href"], reverse=True)
    rule_url = urljoin(base, anchors[0]["href"])
    rule = BeautifulSoup(official_bytes(rule_url).decode("utf-8"), "html.parser")
    match = re.search(r"本通知自\s*(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日起施行", rule.get_text())
    if not match:
        return unknown
    baseline_date = date(*(int(part) for part in match.groups())).isoformat()
    if not baseline_date <= as_of or (date.fromisoformat(as_of) - date.fromisoformat(baseline_date)).days > 180:
        return dict(unknown, baseline_date=baseline_date)
    documents = [urljoin(rule_url, a["href"]) for a in rule.find_all("a", href=True)
                 if a["href"].endswith(".docx") and "标的证券名单" in a.get_text()]
    if not documents:
        return unknown
    with zipfile.ZipFile(io.BytesIO(official_bytes(documents[0]))) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > 8 * 1024 * 1024:
            raise ValueError("oversize list document")
        xml = ElementTree.fromstring(archive.read(info))
    codes = {node.text.strip() for node in xml.iter() if node.text and re.fullmatch(r"[569]\d{5}", node.text.strip())}
    if len(codes) < 100:
        return unknown
    # An absent registered IPO cannot be declared ineligible by this scoped list.
    eligible = True if symbol in codes else None
    evidence_urls, covered, events = [documents[0]], False, []
    list_root = base + "/disclosure/magin/announcement/ssereport/"
    request_count = 4
    last_date = None
    for page in range(2):
        if time.monotonic() > deadline:
            break
        url = list_root + ("s_list.shtml" if page == 0 else "s_list_1.shtml")
        listing = parse_sse_index(official_bytes(url).decode("utf-8"), page + 1)
        request_count += 1
        if listing and last_date and listing[0]["published"] > last_date:
            return dict(unknown, reason="historical_event_pages_unsorted")
        last_date = listing[-1]["published"] if listing else last_date
        for entry in listing:
            if entry["published"] < baseline_date:
                covered = True
                break
            if entry["published"] > as_of or "标的证券调整" not in entry["title"]:
                continue
            if request_count >= 12 or time.monotonic() > deadline:
                return dict(unknown, reason="historical_event_budget_exhausted", baseline_date=baseline_date)
            event_url = urljoin(base, entry["url"])
            event = parse_sse_adjustment(official_bytes(event_url).decode("utf-8"), event_url)
            request_count += 1
            if not event["verified"]:
                return dict(unknown, reason="historical_event_unparsed", evidence_url=event_url)
            if event["effective_date"] <= as_of:
                events.append(event)
        if covered:
            break
    if not covered:
        return dict(unknown, reason="historical_event_window_incomplete", baseline_date=baseline_date)
    for event in sorted(events, key=lambda item: item["effective_date"]):
        if symbol in event["codes"]:
            eligible = event["eligible"]
            evidence_urls.append(event["source_url"])
    if eligible is None:
        return dict(unknown, reason="absent_from_scoped_quarterly_list_not_proof", baseline_date=baseline_date,
                    evidence_urls=evidence_urls)
    return {"verified": True, "eligible": eligible, "source": "SSE", "source_url": rule_url,
            "evidence_date": as_of, "baseline_date": baseline_date, "evidence_urls": evidence_urls,
            "evidence_count": len(codes), "events_checked": len(events), "request_count": request_count,
            "reason": "official_quarterly_membership_with_bounded_adjustment_chain",
            "scope": "quarterly_list_excludes_registration_based_IPOs; no_negative_inference_from_absence"}
