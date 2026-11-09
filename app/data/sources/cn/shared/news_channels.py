"""CN 个股新闻直连通道（跨源共享）。

背景：akshare 的个股新闻接口（stock_news_em 等）在当前环境下解析失败
（pyarrow 正则异常）、tushare news 受积分限制，因此个股公告/新闻依赖
以下直连 HTTP 通道。它们是整个 news 域的实际数据来源，被 akshare 与
tushare 两个 Provider 的 news API 共用——收拢在此避免各源重复实现。

通道：
1. 东方财富个股公告 API（np-anotice-stock）
2. 东方财富搜索 API（search-api-web，jsonp）
3. 新浪财经个股资讯列表（不是不可靠的全市场滚动关键词过滤）
"""

import asyncio
import json
import logging
import re
import time
from html import unescape
from urllib.parse import urlparse
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def is_public_sina_url(url: str, *, include_archive: bool = False) -> bool:
    """Never follow an authenticated, internal, redirected or malformed link."""
    hosts = {"finance.sina.com.cn", "cj.sina.cn"}
    if include_archive:
        hosts.add("vip.stock.finance.sina.com.cn")
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme == "https" and parsed.hostname in hosts
                    and parsed.port in {None, 443} and not parsed.username and not parsed.password)
    except (TypeError, ValueError):
        return False


async def fetch_em_notices(symbol: str, limit: int = 10) -> List[Dict[str, Any]]:
    """东方财富个股公告（精确匹配 symbol，含公告+新闻）"""
    import requests as req

    def _fetch():
        url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
        params = {
            "sr": "-1",
            "page_size": str(min(limit, 20)),
            "page_index": "1",
            "ann_type": "A",
            "client_source": "web",
            "f_node": "0",
            "s_node": "0",
            "stock_list": symbol,
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0.0.0 Safari/537.36",
            "Referer": "https://data.eastmoney.com/notices/stock.html",
        }
        try:
            resp = req.get(url, params=params, headers=headers, timeout=(3, 6))
            resp.raise_for_status()
            data = resp.json()
            notices = data.get("data", {}).get("list", [])
            results = []
            for notice in notices:
                art_code = notice.get("art_code", "")
                title = notice.get("title", "")
                results.append({
                    "title": title,
                    "content": title,
                    "summary": title[:200],
                    "url": f"https://data.eastmoney.com/notices/detail/{symbol}/{art_code}.html",
                    "source": "东方财富公告",
                    "publish_time": notice.get("notice_date", ""),
                    "data_source": "em_notice",
                    "type": "announcement",
                    "symbol": symbol,
                    "source_kind": "aggregator",
                    "content_status": "title_only",
                })
            return results
        except Exception as e:
            logger.debug(f"东方财富公告获取失败: {e}")
            return []

    results = await asyncio.to_thread(_fetch)
    if results:
        logger.info(f"  个股公告 ({symbol}): {len(results)} 条")
    return results


async def fetch_em_search(
    stock_name: str, symbol: str, limit: int = 10, *, end_date: str = None,
) -> List[Dict[str, Any]]:
    """Search by exact security code; names such as 中国电影 are ambiguous."""
    import requests as req

    def _fetch():
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        ts_ms = int(time.time() * 1000)
        cb = f"jQuery{ts_ms}"
        param = json.dumps({
            "uid": "",
            "keyword": symbol,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": min(max(int(limit), 1), 30),
                    "preTag": "",
                    "postTag": "",
                }
            },
        }, ensure_ascii=False)
        params = {"cb": cb, "param": param, "_": str(ts_ms)}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0.0.0 Safari/537.36",
            "Referer": f"https://so.eastmoney.com/news/s?keyword={symbol}",
        }
        try:
            resp = req.get(url, params=params, headers=headers, timeout=(3, 6))
            resp.raise_for_status()
            text = resp.text.strip()
            match = re.search(r"\((.+)\);?$", text, re.DOTALL)
            if not match:
                return []
            data = json.loads(match.group(1))
            articles = data.get("result", {}).get("cmsArticleWebOld") or []
            if isinstance(articles, dict):
                articles = articles.get("list") or []
            results = []
            for art in articles:
                title = unescape(re.sub(r"<[^>]*>", "", str(art.get("title") or ""))).strip()
                content = unescape(re.sub(r"<[^>]*>", "", str(art.get("content") or ""))).replace("　", " ").replace("\r\n", " ")
                if not title:
                    continue
                # 相关性过滤: 标题或内容包含股票名称
                full_text = f"{title} {content}"
                if not ((stock_name and stock_name in full_text) or symbol in full_text):
                    continue
                published = str(art.get("date") or "")
                if end_date and published[:10] > end_date[:10]:
                    continue
                article_code = str(art.get("code") or "")
                article_url = str(art.get("url") or "")
                if not article_url and re.fullmatch(r"\d{10,30}", article_code):
                    article_url = f"https://finance.eastmoney.com/a/{article_code}.html"
                results.append({
                    "title": title,
                    "content": content,
                    "summary": content[:200] if len(content) > 200 else content,
                    "url": article_url,
                    "source": art.get("mediaName", "东方财富"),
                    "publish_time": published,
                    "data_source": "em_search",
                    "type": "news",
                    "symbol": symbol,
                    "source_kind": "aggregator",
                    "content_status": "search_excerpt" if content and content.strip() != title.strip() else "title_only",
                    "discovery_metadata": {"channel": "eastmoney_security_code_search",
                                           "query_symbol": symbol,
                                           "subject_binding": "query_relevance_not_verified_company_fact",
                                           "body_completeness": "search_excerpt_not_full_article"},
                })
            results.sort(key=lambda item: item["publish_time"], reverse=True)
            return results
        except Exception as e:
            logger.debug(f"东方财富搜索失败: {e}")
            return []

    results = await asyncio.to_thread(_fetch)
    if results:
        logger.info(f"  东方财富搜索 ({stock_name}): {len(results)} 条")
    return results


async def fetch_sina_news(
    stock_name: str, symbol: str, limit: int = 10, *, end_date: str = None,
) -> List[Dict[str, Any]]:
    """Bounded stock-specific archive plus at most three public body excerpts.

    A symbol-associated page also contains industry/homonym stories. Require a
    code or explicit company-name title anchor; assignment to this query alone
    is never company identity verification. Failures keep available headlines.
    """
    import requests as req
    from bs4 import BeautifulSoup
    from concurrent.futures import ThreadPoolExecutor

    if not re.fullmatch(r"\d{6}", symbol):
        return []
    prefix = "sh" if symbol.startswith("6") else "bj" if symbol.startswith(("4", "8", "9")) else "sz"
    list_url = f"https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php?symbol={prefix}{symbol}"

    def read_html(url, deadline):
        if not is_public_sina_url(url, include_archive=True):
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0.2:
            return None
        timeout = (min(1.5, remaining / 2), min(2, remaining / 2))
        try:
            with req.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout,
                         stream=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    return None
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    return None
                chunks, size = [], 0
                for chunk in response.iter_content(16384):
                    size += len(chunk)
                    if size > 524288 or time.monotonic() >= deadline:
                        return None
                    chunks.append(chunk)
                return BeautifulSoup(b"".join(chunks), "html.parser")
        except Exception:
            return None

    def body_excerpt(item, deadline):
        soup = read_html(item["url"], deadline)
        if soup is None:
            return {**item, "discovery_metadata": {**item["discovery_metadata"], "body_fetch_status": "failed_or_bounded"}}
        node = soup.select_one("#artibody") or soup.select_one("#article")
        if node is None:
            return {**item, "discovery_metadata": {**item["discovery_metadata"], "body_fetch_status": "body_node_unavailable"}}
        for unwanted in node.select("script,style,iframe"):
            unwanted.decompose()
        text = node.get_text("\n", strip=True)[:6000]
        if len(re.sub(r"\s+", "", text)) < 30 or text.strip() == item["title"].strip():
            return item
        return {**item, "content": text, "summary": text[:200], "content_status": "html_excerpt",
                "discovery_metadata": {**item["discovery_metadata"], "body_fetch_status": "public_html_excerpt",
                                       "body_completeness": "bounded_excerpt_not_verified_full_article"}}

    def _fetch():
        try:
            deadline = time.monotonic() + 7
            soup = read_html(list_url, deadline)
            listing = soup.select_one(".datelist") if soup else None
            if listing is None:
                return []
            results = []
            code = re.compile(r"(?<!\d)" + re.escape(symbol) + r"(?!\d)")
            company = re.compile(r"^" + re.escape(stock_name) + r"\s*[:：（(]") if stock_name else None
            for link in listing.select("a[href]")[:100]:
                title = link.get_text(" ", strip=True)
                if not code.search(title) and not (company and company.search(title)):
                    continue
                stamp = re.search(r"20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}", str(link.previous_sibling))
                if not stamp:
                    continue
                publish_time = re.sub(r"\s+", " ", stamp[0]) + ":00"
                if end_date and publish_time[:10] > end_date[:10]:
                    continue
                article_url = str(link.get("href") or "")
                if not is_public_sina_url(article_url):
                    continue
                results.append({
                    "title": title,
                    "content": title,
                    "summary": title,
                    "url": article_url,
                    "source": "新浪财经个股资讯",
                    "publish_time": publish_time,
                    "data_source": "sina_stock_archive",
                    "type": "news",
                    "symbol": symbol,
                    "source_kind": "aggregator", "content_status": "title_only",
                    "discovery_metadata": {"channel": "sina_stock_archive", "list_url": list_url,
                                           "subject_binding": "explicit_company_title_not_fact_verified",
                                           "automated_article": "/aiassist/" in article_url or "/aigc/" in article_url},
                })
                if len(results) >= min(max(int(limit), 1), 30):
                    break
            results.sort(key=lambda item: item["publish_time"], reverse=True)
            if results and time.monotonic() < deadline - 0.2:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    enriched = list(pool.map(lambda item: body_excerpt(item, deadline), results[:3]))
                results[:len(enriched)] = enriched
            return results
        except Exception as e:
            logger.debug("新浪个股资讯暂不可用: %s", type(e).__name__)
            return []

    results = await asyncio.to_thread(_fetch)
    if results:
        logger.info(f"  新浪个股资讯 ({stock_name}): {len(results)} 条")
    return results
